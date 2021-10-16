# Copyright (C) 2019-2021 Unrud <unrud@outlook.com>
#
# This file is part of Video Downloader.
#
# Video Downloader is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Video Downloader is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Video Downloader.  If not, see <http://www.gnu.org/licenses/>.

import contextlib
import fcntl
import glob
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import dfxp2srt, sanitize_filename

# File names are typically limited to 255 bytes
MAX_OUTPUT_TITLE_LENGTH = 200
MAX_THUMBNAIL_RESOLUTION = 1024
FFMPEG_EXE = 'ffmpeg'


def _short_filename(name, length):
    for i in range(len(name), -1, -1):
        output = name[:i]
        if i < len(name):
            output += '…'
        output = sanitize_filename(output)
        # Check length with file system encoding
        if (len(output.encode(sys.getfilesystemencoding(), 'ignore'))
                < length):
            break
    return output


@contextlib.contextmanager
def _create_and_lock_dir(dirpath):
    for i in itertools.count():
        if i > 0:
            time.sleep(0.5)
        os.makedirs(dirpath, exist_ok=True)
        try:
            fd = os.open(dirpath, 0)
        except FileNotFoundError:
            continue
        try:
            # Acquire lock on directory
            fcntl.flock(fd, fcntl.LOCK_EX)
            stat = os.fstat(fd)
            # Check that the directory still exists and is the same
            try:
                fd_cmp = os.open(dirpath, 0)
            except FileNotFoundError:
                continue
            try:
                stat_cmp = os.fstat(fd_cmp)
            finally:
                os.close(fd_cmp)
            if (stat.st_dev != stat_cmp.st_dev or
                    stat.st_ino != stat_cmp.st_ino):
                continue
            yield
            break
        finally:
            os.close(fd)


class RetryException(BaseException):
    pass


class YoutubeDLSlave:
    def _on_progress(self, d):
        if d['status'] not in ['downloading', 'finished']:
            return
        filename = d['filename']
        bytes_ = d.get('downloaded_bytes')
        if bytes_ is None:
            bytes_ = -1
        bytes_total = d.get('total_bytes')
        if bytes_total is None:
            bytes_total = d.get('total_bytes_estimate')
        if bytes_total is None:
            bytes_total = -1
        if d['status'] == 'downloading':
            fragments = d.get('fragment_index')
            if fragments is None:
                fragments = -1
            fragments_total = d.get('fragment_count')
            if fragments_total is None:
                fragments_total = -1
            if bytes_ >= 0 and bytes_total >= 0:
                progress = bytes_ / bytes_total if bytes_total > 0 else -1
            elif fragments >= 0 and fragments_total >= 0:
                progress = (fragments / fragments_total
                            if fragments_total > 0 else -1)
            else:
                progress = -1
            eta = d.get('eta')
            if eta is None:
                eta = -1
            speed = d.get('speed')
            if speed is None:
                speed = -1
            speed = round(speed)
        elif d['status'] == 'finished':
            progress = -1
            eta = -1
            speed = -1
        self._handler.on_load_progress(
            filename, progress, bytes_, bytes_total, eta, speed)

    def _load_playlist(self, dir_, url):
        '''Retrieve info for all videos available on URL.

        Returns the absolute paths of the generated and downloaded files:
        ([(info json, [thumbnail, ...],
         [(subtitle, lang, ext), ...]), ...], skipped videos)
        '''
        os.chdir(dir_)
        while True:
            ydl_opts = {**self.ydl_opts,
                        'writeinfojson': True,
                        'skip_download': True,
                        'writethumbnail': True,
                        'writesubtitles': True,
                        'outtmpl': '%(autonumber)s.%(ext)s'}
            try:
                saved_skipped_count = self._skipped_count
                yt_dlp.YoutubeDL(ydl_opts).download([url])
            except RetryException:
                continue
            break
        results = []
        dir_listing = os.listdir()
        for name in dir_listing:
            # Info Json: 00001.info.json
            if not re.fullmatch(r'[0-9]+\.info\.json', name):
                continue
            name_root = name.partition('.')[0]
            thumbnails = []
            subtitles = []
            for name2 in dir_listing:
                # Thumbnails: 00001.jpg or 00001_0.jpg
                if re.fullmatch(r'%s(_[0-9]+)?\.[^.]+' % name_root, name2):
                    thumbnails.append(os.path.abspath(name2))
                # Subtitles: 00001.en.vtt
                if re.fullmatch(r'%s\.[^.]+\.[^.]+' % name_root, name2):
                    name2_ext = name2[len(name_root):]
                    if name2_ext != '.info.json':
                        sub_lang, sub_ext = name2_ext[1:].split('.')
                        subtitles.append((os.path.abspath(name2),
                                          sub_lang, sub_ext))
            results.append((os.path.abspath(name), thumbnails, subtitles))
        results.sort(key=lambda result: result[0])
        return (results, self._skipped_count - saved_skipped_count)

    def _load_video(self, dir_, info_path):
        class GetFilepathPP(PostProcessor):
            def run(self, info):
                nonlocal filepath
                filepath = info['filepath']
                return [], info
        filepath = None
        os.chdir(dir_)
        while True:
            try:
                with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                    ydl.add_post_processor(GetFilepathPP())
                    ydl.download_with_info_file(info_path)
            except RetryException:
                continue
            break
        return os.path.abspath(filepath)

    def debug(self, msg):
        print(msg, file=sys.stderr, flush=True)

    def warning(self, msg):
        print(msg, file=sys.stderr, flush=True)

    def error(self, msg):
        print(msg, file=sys.stderr, flush=True)
        # Handle authentication requests
        if (self._allow_authentication_request and
                re.search(r'\b[Ss]ign in\b|--username', msg)):
            if self._skip_authentication:
                self._skipped_count += 1
                return
            user, password = self._handler.on_login_request()
            if not user and not password:
                self._skip_authentication = True
                self._skipped_count += 1
                return
            self.ydl_opts['username'] = user
            self.ydl_opts['password'] = password
            self._allow_authentication_request = False
            raise RetryException(msg)
        if self._allow_authentication_request and '--video-password' in msg:
            if self._skip_authentication:
                self._skipped_count += 1
                return
            videopassword = self._handler.on_videopassword_request()
            if not videopassword:
                self._skip_authentication = True
                self._skipped_count += 1
                return
            self.ydl_opts['videopassword'] = videopassword
            self._allow_authentication_request = False
            raise RetryException(msg)
        # Ignore missing xattr support
        if 'This filesystem doesn\'t support extended attributes.' in msg:
            return
        self._handler.on_error(msg)
        sys.exit(1)

    @staticmethod
    def _find_existing_download(download_dir, output_title, mode):
        for filepath in glob.iglob(glob.escape(
                os.path.join(download_dir, output_title)) + '.*'):
            filename = os.path.basename(filepath)
            file_title, file_ext = os.path.splitext(filename)
            if file_title == output_title and (
                    mode == 'audio' and file_ext.lower() == '.mp3' or
                    mode != 'audio' and file_ext.lower() != '.mp3') and (
                    os.path.isfile(filepath)):
                return filename
        return None

    def __init__(self, handler):
        self._handler = handler
        self._allow_authentication_request = True
        self._skip_authentication = False
        self._skipped_count = 0
        self.ydl_opts = {
            'logger': self,
            'logtostderr': True,
            'no_color': True,
            'progress_hooks': [self._on_progress],
            'fixup': 'detect_or_warn',
            'ignoreerrors': True,  # handled via logger error callback
            'retries': 10,
            'fragment_retries': 10,
            'writesubtitles': True,
            'subtitleslangs': ['all'],
            'subtitlesformat': 'vtt/best',
            'keepvideo': True,
            # Include id and format_id in outtmpl to prevent youtube-dl
            # from continuing wrong file
            'outtmpl': '%(id)s.%(format_id)s.%(ext)s',
            'postprocessors': [
                {'key': 'FFmpegMetadata'},
                {'key': 'FFmpegEmbedSubtitle'},
                {'key': 'XAttrMetadata'}]}
        mode = self._handler.get_mode()
        if mode == 'audio':
            self.ydl_opts['format'] = 'bestaudio/best'
            self.ydl_opts['postprocessors'].insert(0, {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192'})
            self.ydl_opts['postprocessors'].insert(1, {
                'key': 'EmbedThumbnail',
                'already_have_thumbnail': True})
        else:
            self.ydl_opts['format_sort'] = [
                'res~%d' % self._handler.get_resolution()]
            if self._handler.get_prefer_mpeg():
                self.ydl_opts['format_sort'].append('+codec:avc:m4a')
        url = self._handler.get_url()
        download_dir = os.path.abspath(self._handler.get_download_dir())
        with tempfile.TemporaryDirectory() as temp_dir:
            self.ydl_opts['cookiefile'] = os.path.join(temp_dir, 'cookies')
            # Collect info without downloading videos
            testplaylist_dir = os.path.join(temp_dir, 'testplaylist')
            noplaylist_dir = os.path.join(temp_dir, 'noplaylist')
            fullplaylist_dir = os.path.join(temp_dir, 'fullplaylist')
            for path in [testplaylist_dir, noplaylist_dir, fullplaylist_dir]:
                os.mkdir(path)
            self.ydl_opts['playlistend'] = 2
            # Test playlist
            info_testplaylist, skipped_testplaylist = self._load_playlist(
                testplaylist_dir, url)
            self.ydl_opts['noplaylist'] = True
            if len(info_testplaylist) + skipped_testplaylist > 1:
                info_noplaylist, skipped_noplaylist = self._load_playlist(
                    noplaylist_dir, url)
            else:
                info_noplaylist = info_testplaylist
                skipped_noplaylist = skipped_testplaylist
            del self.ydl_opts['noplaylist']
            del self.ydl_opts['playlistend']
            if (len(info_testplaylist) + skipped_testplaylist >
                    len(info_noplaylist) + skipped_noplaylist):
                self.ydl_opts['noplaylist'] = (
                    not self._handler.on_playlist_request())
                if not self.ydl_opts['noplaylist']:
                    info_playlist, _ = self._load_playlist(
                        fullplaylist_dir, url)
                else:
                    info_playlist = info_noplaylist
            elif len(info_testplaylist) + skipped_testplaylist > 1:
                info_playlist, _ = self._load_playlist(fullplaylist_dir, url)
            else:
                info_playlist = info_testplaylist
            # Download videos
            self._allow_authentication_request = False
            try:
                os.makedirs(download_dir, exist_ok=True)
            except OSError as e:
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()
                self._handler.on_error(
                    'ERROR: Failed to create download folder: %s' % e)
                sys.exit(1)
            for i, (info_path, thumbnail_paths, subtitles) in enumerate(
                    info_playlist):
                with open(info_path) as f:
                    info = json.load(f)
                title = info.get('title') or info.get('id') or 'video'
                output_title = _short_filename(title, MAX_OUTPUT_TITLE_LENGTH)
                # Convert subtitles to VTT format
                # youtube-dl fails for subtitles that it can't convert or
                # are unsupported by ffmpeg
                new_subtitles = {}
                for sub_path, sub_lang, sub_ext in subtitles:
                    print('[youtube_dl_slave] Testing subtitle (%r, %r)' %
                          (sub_lang, sub_ext), file=sys.stderr, flush=True)
                    if sub_ext in ['dfxp', 'ttml', 'tt']:
                        # Try to use youtube-dl's internal dfxp2srt converter
                        with open(sub_path, 'rb') as f:
                            sub_data = f.read()
                        try:
                            sub_data = dfxp2srt(sub_data)
                        except Exception:
                            traceback.print_exc(file=sys.stderr)
                            sys.stderr.flush()
                            continue
                        sub_path += '-converted.srt'
                        with open(sub_path, 'w', encoding='utf-8') as f:
                            f.write(sub_data)
                    # Try to convert subtitles with ffmpeg
                    try:
                        sub_data_vtt = subprocess.run(
                            [FFMPEG_EXE, '-i', os.path.abspath(sub_path),
                             '-f', 'webvtt', '-'],
                            check=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE).stdout.decode('utf-8')
                    except FileNotFoundError:
                        traceback.print_exc(file=sys.stderr)
                        sys.stderr.flush()
                        self._handler.on_error(
                            'ERROR: %r not found' % FFMPEG_EXE)
                        sys.exit(1)
                    except subprocess.CalledProcessError:
                        traceback.print_exc(file=sys.stderr)
                        sys.stderr.flush()
                        continue
                    new_subtitles[sub_lang] = [{
                        **info['subtitles'][sub_lang][-1],
                        'ext': 'vtt',
                        'data': sub_data_vtt
                    }]
                info['subtitles'] = new_subtitles
                thumbnail_path = thumbnail_paths[0] if thumbnail_paths else ''
                new_thumbnails = []
                if thumbnail_path:
                    # Convert thumbnail to JPEG and limit resolution
                    print('[youtube_dl_slave] Converting thumbnail',
                          file=sys.stderr, flush=True)
                    new_thumbnail_path = thumbnail_path + '-converted.jpg'
                    try:
                        subprocess.run(
                            [FFMPEG_EXE, '-i', os.path.abspath(thumbnail_path),
                             '-vf', ('scale=\'min({0},iw):min({0},ih):'
                                     'force_original_aspect_ratio=decrease\''
                                     ).format(MAX_THUMBNAIL_RESOLUTION),
                             os.path.abspath(new_thumbnail_path)],
                            check=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL)
                    except FileNotFoundError:
                        traceback.print_exc(file=sys.stderr)
                        sys.stderr.flush()
                        self._handler.on_error(
                            'ERROR: %r not found' % FFMPEG_EXE)
                        sys.exit(1)
                    except subprocess.CalledProcessError:
                        traceback.print_exc(file=sys.stderr)
                        sys.stderr.flush()
                        new_thumbnail_path = ''
                    # No longer needed
                    os.remove(thumbnail_path)
                    thumbnail_path = new_thumbnail_path
                    new_thumbnails.append({**info['thumbnails'][-1],
                                           'filename': thumbnail_path})
                info['thumbnails'] = new_thumbnails
                self._handler.on_progress_start(i, len(info_playlist), title,
                                                thumbnail_path)
                # Remove description, because long comments cause problems when
                # displayed in Nautilus and other applications.
                with contextlib.suppress(KeyError):
                    del info['description']
                with open(info_path, 'w') as f:
                    json.dump(info, f)
                # Check if we already got the file
                existing_filename = self._find_existing_download(
                    download_dir, output_title, mode)
                if existing_filename is not None:
                    self._handler.on_progress_end(existing_filename)
                    continue
                # Download into separate directory because youtube-dl generates
                # many temporary files
                temp_download_dir = os.path.join(
                    download_dir, output_title + '.part')
                # Lock download directory to prevent other processes from
                # writing to the same files
                temp_download_dir_cm = contextlib.ExitStack()
                try:
                    temp_download_dir_cm.enter_context(
                        _create_and_lock_dir(temp_download_dir))
                except OSError as e:
                    traceback.print_exc(file=sys.stderr)
                    sys.stderr.flush()
                    self._handler.on_error(
                        'ERROR: Failed to lock download folder: %s' % e)
                    sys.exit(1)
                with temp_download_dir_cm:
                    # Check if the file got downloaded in the meantime
                    existing_filename = self._find_existing_download(
                        download_dir, output_title, mode)
                    if existing_filename is not None:
                        filename = existing_filename
                    else:
                        temp_filepath = self._load_video(temp_download_dir,
                                                         info_path)
                        _, filename_ext = os.path.splitext(temp_filepath)
                        filename = output_title + filename_ext
                        # Move finished download from download to target dir
                        try:
                            os.replace(temp_filepath,
                                       os.path.join(download_dir, filename))
                        except OSError as e:
                            traceback.print_exc(file=sys.stderr)
                            sys.stderr.flush()
                            self._handler.on_error((
                                'ERROR: Falied to move finished download to '
                                'download folder: %s') % e)
                            sys.exit(1)
                    # Delete download directory
                    with contextlib.suppress(OSError):
                        shutil.rmtree(temp_download_dir)
                self._handler.on_progress_end(filename)
