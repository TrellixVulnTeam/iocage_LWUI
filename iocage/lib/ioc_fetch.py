# Copyright (c) 2014-2017, iocage
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""iocage fetch module."""
import collections
import distutils.dir_util
import ftplib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess as su
import sys
import tarfile
import tempfile
import urllib.request

import libzfs
import pygit2
import requests
import requests.auth
import requests.packages.urllib3.exceptions
import texttable
import tqdm

import iocage.lib.ioc_common
import iocage.lib.ioc_create
import iocage.lib.ioc_destroy
import iocage.lib.ioc_exec
import iocage.lib.ioc_json
import iocage.lib.ioc_start


class IOCFetch(object):
    """Fetch a RELEASE for use as a jail base."""

    def __init__(self, release, server="ftp.freebsd.org", user="anonymous",
                 password="anonymous@", auth=None, root_dir=None, http=False,
                 _file=False, verify=True, hardened=False, update=True,
                 eol=True, files=("MANIFEST", "base.txz", "lib32.txz",
                                  "doc.txz"), exit_on_error=False,
                 silent=False, callback=None, plugin=None):
        self.pool = iocage.lib.ioc_json.IOCJson(
            exit_on_error=exit_on_error).json_get_value("pool")
        self.iocroot = iocage.lib.ioc_json.IOCJson(
            self.pool, exit_on_error=exit_on_error).json_get_value("iocroot")
        self.server = server
        self.user = user
        self.password = password
        self.auth = auth

        if release:
            self.release = release.upper()
        else:
            self.release = release

        self.root_dir = root_dir
        self.arch = os.uname()[4]
        self.http = http
        self._file = _file
        self.verify = verify
        self.hardened = hardened
        self.files = files
        self.update = update
        self.eol = eol
        self.exit_on_error = exit_on_error
        self.silent = silent
        self.callback = callback

        self.zfs = libzfs.ZFS(history=True, history_prefix="<iocage>")
        self.zpool = self.zfs.get(self.pool)
        self.plugin = plugin

        if hardened:
            self.http = True

            if release:
                self.release = f"{self.release[:2]}-stable".upper()
            else:
                self.release = release

        if not verify:
            # The user likely knows this already.
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning)

    @staticmethod
    def __fetch_eol_check__():
        """Scrapes the FreeBSD website and returns a list of EOL RELEASES"""
        logging.getLogger("requests").setLevel(logging.WARNING)
        _eol = "https://www.freebsd.org/security/unsupported.html"
        req = requests.get(_eol)
        status = req.status_code == requests.codes.ok
        eol_releases = []
        if not status:
            req.raise_for_status()

        for eol in req.content.decode("iso-8859-1").split():
            eol = eol.strip("href=").strip("/").split(">")
            # We want a dynamic EOL
            try:
                if "-RELEASE" in eol[1]:
                    eol = eol[1].strip('</td')
                    if eol not in eol_releases:
                        eol_releases.append(eol)
            except IndexError:
                pass

        return eol_releases

    def __fetch_host_release__(self):
        """Helper to return the hosts sanitized RELEASE"""
        rel = os.uname()[2]
        self.release = rel.rsplit("-", 1)[0]

        if "-STABLE" in rel:
            # FreeNAS
            self.release = f"{self.release}-RELEASE"
        elif "-HBSD" in rel:
            # HardenedBSD
            self.release = re.sub(r"\W\w.", "-", self.release)
            self.release = re.sub(r"([A-Z])\w+", "STABLE", self.release)
        elif "-RELEASE" not in rel:
            self.release = "Not a RELEASE"

        return self.release

    def __fetch_validate_release__(self, releases):
        """
        Checks if the user supplied an index number and returns the
        RELEASE. If they gave us a full RELEASE, we make sure that exists in
        the list at all.
        """
        if self.release.lower() == "exit":
            exit()

        if len(self.release) > 2:
            # Quick list validation
            try:
                releases.index(self.release)
            except ValueError as err:
                iocage.lib.ioc_common.logit({
                    "level"  : "EXCEPTION",
                    "message": err
                }, exit_on_error=self.exit_on_error, _callback=self.callback,
                    silent=self.silent)
            return self.release

        try:
            self.release = releases[int(self.release)]
        except IndexError:
            iocage.lib.ioc_common.logit({
                "level"  : "EXCEPTION",
                "message": f"[{self.release}] is not in the list!"
            }, exit_on_error=self.exit_on_error, _callback=self.callback,
                silent=self.silent)
        except ValueError:
            # We want to use their host as RELEASE, but it may
            # not be on the mirrors anymore.
            try:
                self.release = self.__fetch_host_release__()

                if "-STABLE" in self.release:
                    # Custom HardenedBSD server
                    self.hardened = True
                    return self.release

                releases.index(self.release)
            except ValueError:
                iocage.lib.ioc_common.logit({
                    "level"  : "EXCEPTION",
                    "message": "Please select an item!"
                }, exit_on_error=self.exit_on_error, _callback=self.callback,
                    silent=self.silent)
        return self.release

    def fetch_release(self, _list=False):
        """Small wrapper to choose the right fetch."""
        if self.http:
            if self.eol and self.verify:
                eol = self.__fetch_eol_check__()
            else:
                eol = []

            self.fetch_http_release(eol, _list=_list)
        elif self._file:
            # Format for file directory should be: root-dir/RELEASE/*.txz
            if not self.root_dir:
                iocage.lib.ioc_common.logit({
                    "level"  : "EXCEPTION",
                    "message": "Please supply --root-dir or -d."
                }, exit_on_error=self.exit_on_error, _callback=self.callback,
                    silent=self.silent)

            if self.release is None:
                iocage.lib.ioc_common.logit({
                    "level"  : "EXCEPTION",
                    "message": "Please supply a RELEASE!"
                }, exit_on_error=self.exit_on_error, _callback=self.callback,
                    silent=self.silent)

            try:
                os.chdir(f"{self.root_dir}/{self.release}")
            except OSError as err:
                iocage.lib.ioc_common.logit({
                    "level"  : "EXCEPTION",
                    "message": err
                }, exit_on_error=self.exit_on_error, _callback=self.callback,
                    silent=self.silent)

            dataset = f"{self.iocroot}/download/{self.release}"

            if os.path.isdir(dataset):
                pass
            else:
                self.zpool.create(dataset, {
                    "compression": "lz4"
                })

                self.zfs.get_dataset_by_path(dataset).mount()

            for f in self.files:
                if not os.path.isfile(f):

                    dataset = self.zfs.get_dataset(f"{self.pool}{dataset}")

                    dataset.unmount()
                    dataset.delete(recursive=True)

                    if f == "MANIFEST":
                        error = f"{f} is a required file!" \
                                f"\nPlease place it in {self.root_dir}/" \
                                f"{self.release}"
                    else:
                        error = f"{f}.txz is a required file!" \
                                f"\nPlease place it in {self.root_dir}/" \
                                f"{self.release}"

                    iocage.lib.ioc_common.logit({
                        "level"  : "EXCEPTION",
                        "message": error
                    }, exit_on_error=self.exit_on_error,
                        _callback=self.callback,
                        silent=self.silent)

                iocage.lib.ioc_common.logit({
                    "level"  : "INFO",
                    "message": f"Copying: {f}... "
                }, _callback=self.callback, silent=self.silent)
                shutil.copy(f, dataset)

                if f != "MANIFEST":
                    iocage.lib.ioc_common.logit({
                        "level"  : "INFO",
                        "message": f"Extracting: {f}... "
                    },
                        _callback=self.callback,
                        silent=self.silent)
                    self.fetch_extract(f)
        else:
            if self.eol and self.verify:
                eol = self.__fetch_eol_check__()
            else:
                eol = []

            self.fetch_ftp_release(eol, _list=_list)

    def fetch_http_release(self, eol, _list=False):
        """
        Fetch a user specified RELEASE from FreeBSD's http server or a user
        supplied one. The user can also specify the user, password and
        root-directory containing the release tree that looks like so:
            - XX.X-RELEASE
            - XX.X-RELEASE
            - XX.X_RELEASE
        """
        if self.hardened:
            if self.server == "ftp.freebsd.org":
                self.server = "http://jenkins.hardenedbsd.org"
                rdir = "builds"

        if self.server == "ftp.freebsd.org":
            self.server = "https://download.freebsd.org"

        if self.root_dir is None:
            self.root_dir = f"ftp/releases/{self.arch}"

        if self.auth and "https" not in self.server:
            self.server = "https://" + self.server
        elif "http" not in self.server:
            self.server = "http://" + self.server

        logging.getLogger("requests").setLevel(logging.WARNING)

        if self.hardened:
            if self.auth == "basic":
                req = requests.get(f"{self.server}/{rdir}",
                                   auth=(self.user, self.password),
                                   verify=self.verify)
            elif self.auth == "digest":
                req = requests.get(f"{self.server}/{rdir}",
                                   auth=requests.auth.HTTPDigestAuth(
                                       self.user, self.password),
                                   verify=self.verify)
            else:
                req = requests.get(f"{self.server}/{rdir}")

            releases = []
            status = req.status_code == requests.codes.ok
            if not status:
                req.raise_for_status()

            if not self.release:
                for rel in req.content.split():
                    rel = rel.decode()
                    rel = rel.strip("href=").strip("/").split(">")

                    if "-STABLE" in rel[0]:
                        rel = rel[0].strip('"').strip("/").strip(
                            "HardenedBSD-").rsplit("-")
                        rel = f"{rel[0]}-{rel[1]}"

                        if rel not in releases:
                            releases.append(rel)

                releases = iocage.lib.ioc_common.sort_release(releases)

                for r in releases:
                    iocage.lib.ioc_common.logit({
                        "level"  : "INFO",
                        "message": f"[{releases.index(r)}] {r}"
                    },
                        _callback=self.callback,
                        silent=self.silent)
                host_release = self.__fetch_host_release__()
                self.release = input("\nType the number of the desired"
                                     " RELEASE\nPress [Enter] to fetch "
                                     f"the default selection: ({host_release})"
                                     "\nType EXIT to quit: ")
                self.release = self.__fetch_validate_release__(releases)
        else:
            if self.auth == "basic":
                req = requests.get(f"{self.server}/{self.root_dir}",
                                   auth=(self.user, self.password),
                                   verify=self.verify)
            elif self.auth == "digest":
                req = requests.get(f"{self.server}/{self.root_dir}",
                                   auth=requests.auth.HTTPDigestAuth(
                                       self.user, self.password),
                                   verify=self.verify)
            else:
                req = requests.get(f"{self.server}/{self.root_dir}")

            releases = []
            status = req.status_code == requests.codes.ok
            if not status:
                req.raise_for_status()

            if not self.release:
                for rel in req.content.split():
                    rel = rel.decode()
                    rel = rel.strip("href=").strip("/").split(">")
                    if "-RELEASE" in rel[0]:
                        rel = rel[0].strip('"').strip("/").strip(
                            "/</a").strip('title="')
                        if rel not in releases:
                            releases.append(rel)

                releases = iocage.lib.ioc_common.sort_release(releases)
                for r in releases:
                    if r in eol:
                        iocage.lib.ioc_common.logit({
                            "level"  : "INFO",
                            "message": f"[{releases.index(r)}] {r} (EOL)"
                        },
                            _callback=self.callback,
                            silent=self.silent)
                    else:
                        iocage.lib.ioc_common.logit({
                            "level"  : "INFO",
                            "message": f"[{releases.index(r)}] {r}"
                        },
                            _callback=self.callback,
                            silent=self.silent)

                if _list:
                    return

                host_release = self.__fetch_host_release__()
                self.release = input("\nType the number of the desired"
                                     " RELEASE\nPress [Enter] to fetch "
                                     f"the default selection: ({host_release})"
                                     "\nType EXIT to quit: ")
                self.release = self.__fetch_validate_release__(releases)

        if self.hardened:
            self.root_dir = f"{rdir}/HardenedBSD-{self.release.upper()}-" \
                            f"{self.arch}-LATEST"
        iocage.lib.ioc_common.logit({
            "level"  : "INFO",
            "message": f"Fetching: {self.release}\n"
        },
            _callback=self.callback,
            silent=self.silent)
        self.fetch_download(self.files)
        missing = self.__fetch_check__(self.files)

        if missing:
            self.fetch_download(missing, missing=True)
            self.__fetch_check__(missing, _missing=True)

        if not self.hardened:
            self.fetch_update()

    def fetch_ftp_release(self, eol, _list=False):
        """
        Fetch a user specified RELEASE from FreeBSD's ftp server or a user
        supplied one. The user can also specify the user, password and
        root-directory containing the release tree that looks like so:
            - XX.X-RELEASE
            - XX.X-RELEASE
            - XX.X_RELEASE
        """

        ftp = self.__fetch_ftp_connect__()
        ftp_list = ftp.nlst()

        if not self.release:
            ftp_list = [rel for rel in ftp_list if "-RELEASE" in rel]
            releases = iocage.lib.ioc_common.sort_release(ftp_list)

            for r in releases:
                if r in eol:
                    iocage.lib.ioc_common.logit({
                        "level"  : "INFO",
                        "message": f"[{releases.index(r)}] {r} (EOL)"
                    },
                        _callback=self.callback,
                        silent=self.silent)
                else:
                    iocage.lib.ioc_common.logit({
                        "level"  : "INFO",
                        "message": f"[{releases.index(r)}] {r}"
                    },
                        _callback=self.callback,
                        silent=self.silent)

            if _list:
                return

            host_release = self.__fetch_host_release__()
            self.release = input("\nType the number of the desired"
                                 " RELEASE\nPress [Enter] to fetch"
                                 f" the default selection: ({host_release})"
                                 "\nType EXIT to quit: ")

            self.release = self.__fetch_validate_release__(releases)

        # This has the benefit of giving us a list of files, but also as a
        # easy sanity check for the existence of the RELEASE before we reuse
        #  it below.
        try:
            ftp.cwd(self.release)
        except ftplib.error_perm:
            iocage.lib.ioc_common.logit({
                "level"  : "EXCEPTION",
                "message": f"{self.release} was not found!"
            }, exit_on_error=self.exit_on_error, _callback=self.callback,
                silent=self.silent)

        ftp_list = self.files
        ftp.quit()

        iocage.lib.ioc_common.logit({
            "level"  : "INFO",
            "message": f"Fetching: {self.release}\n"
        },
            _callback=self.callback,
            silent=self.silent)
        self.fetch_download(ftp_list, ftp=True)
        missing = self.__fetch_check__(ftp_list, ftp=True)

        if missing:
            self.fetch_download(missing, ftp=True, missing=True)
            self.__fetch_check__(missing, ftp=True, _missing=True)

        if self.update:
            self.fetch_update()

    def __fetch_ftp_connect__(self):
        """
        Connects to the ftp server and returns the proper cwd for easy
        reconnection.
        """
        ftp = ftplib.FTP(self.server)
        ftp.connect()
        ftp.login(user=self.user, passwd=self.password)

        if self.server == "ftp.freebsd.org":
            try:
                ftp.cwd(f"/pub/FreeBSD/releases/{self.arch}")
            except:
                iocage.lib.ioc_common.logit({
                    "level"  : "EXCEPTION",
                    "message": f"{self.arch} was not found!"
                }, exit_on_error=self.exit_on_error, _callback=self.callback,
                    silent=self.silent)
        elif self.root_dir:
            try:
                ftp.cwd(self.root_dir)
            except:
                iocage.lib.ioc_common.logit({
                    "level"  : "EXCEPTION",
                    "message": f"{self.root_dir} was not found!"
                }, exit_on_error=self.exit_on_error, _callback=self.callback,
                    silent=self.silent)

        return ftp

    def __fetch_check__(self, _list, ftp=False, _missing=False):
        """
        Will check if every file we need exists, if they do we check the SHA256
        and make sure it matches the files they may already have.
        """
        hashes = {}
        missing = []

        if os.path.isdir(f"{self.iocroot}/download/{self.release}"):
            os.chdir(f"{self.iocroot}/download/{self.release}")

            for _, _, files in os.walk("."):
                if "MANIFEST" not in files:
                    if ftp and self.server == "ftp.freebsd.org":
                        _ftp = self.__fetch_ftp_connect__()
                        _ftp.cwd(self.release)
                        _ftp.retrbinary("RETR MANIFEST", open("MANIFEST",
                                                              "wb").write)
                        _ftp.quit()
                    elif not ftp and self.server == \
                            "https://download.freebsd.org":
                        r = requests.get(f"{self.server}/{self.root_dir}/"
                                         f"{self.release}/MANIFEST",
                                         verify=self.verify, stream=True)

                        status = r.status_code == requests.codes.ok
                        if not status:
                            r.raise_for_status()

                        with open("MANIFEST", "wb") as txz:
                            shutil.copyfileobj(r.raw, txz)

            try:
                with open("MANIFEST", "r") as _manifest:
                    for line in _manifest:
                        col = line.split("\t")
                        hashes[col[0]] = col[1]
            except FileNotFoundError:
                missing.append("MANIFEST")

            for f in self.files:
                if f == "MANIFEST":
                    continue

                if self.hardened and f == "lib32.txz":
                    continue

                # Python Central
                hash_block = 65536
                sha256 = hashlib.sha256()

                if f in _list:
                    try:
                        with open(f, "rb") as txz:
                            buf = txz.read(hash_block)

                            while len(buf) > 0:
                                sha256.update(buf)
                                buf = txz.read(hash_block)

                            if hashes[f] != sha256.hexdigest():
                                if not _missing:
                                    iocage.lib.ioc_common.logit({
                                        "level"  : "WARNING",
                                        "message": f"{f} failed verification,"
                                                   " will redownload!"
                                    },
                                        _callback=self.callback,
                                        silent=self.silent)
                                    missing.append(f)
                                else:
                                    iocage.lib.ioc_common.logit({
                                        "level"  : "EXCEPTION",
                                        "message": "Too many failed"
                                                   " verifications!"
                                    }, exit_on_error=self.exit_on_error,
                                        _callback=self.callback,
                                        silent=self.silent)
                    except FileNotFoundError:
                        if not _missing:
                            iocage.lib.ioc_common.logit({
                                "level"  : "WARNING",
                                "message":
                                    f"{f} missing, will try to redownload!"
                            },
                                _callback=self.callback,
                                silent=self.silent)
                            missing.append(f)
                        else:
                            iocage.lib.ioc_common.logit({
                                "level"  : "EXCEPTION",
                                "message": "Too many failed verifications!"
                            }, exit_on_error=self.exit_on_error,
                                _callback=self.callback,
                                silent=self.silent)

                if not missing:
                    iocage.lib.ioc_common.logit({
                        "level"  : "INFO",
                        "message": f"Extracting: {f}... "
                    },
                        _callback=self.callback,
                        silent=self.silent)

                    try:
                        self.fetch_extract(f)
                    except:
                        raise

            return missing

    def fetch_download(self, _list, ftp=False, missing=False):
        """Creates the download dataset and then downloads the RELEASE."""
        dataset = f"{self.iocroot}/download/{self.release}"
        fresh = False

        if not os.path.isdir(dataset):
            fresh = True
            dataset = f"{self.pool}/iocage/download/{self.release}"
            self.zpool.create(dataset, {
                "compression": "lz4"
            })

            self.zfs.get_dataset(dataset).mount()

        if missing or fresh:
            os.chdir(f"{self.iocroot}/download/{self.release}")

            if self.http:
                for f in _list:
                    if self.hardened:
                        _file = f"{self.server}/{self.root_dir}/{f}"
                        if f == "lib32.txz":
                            continue
                    else:
                        _file = f"{self.server}/{self.root_dir}/" \
                                f"{self.release}/{f}"
                    if self.auth == "basic":
                        r = requests.get(_file,
                                         auth=(self.user, self.password),
                                         verify=self.verify, stream=True)
                    elif self.auth == "digest":
                        r = requests.get(_file,
                                         auth=requests.auth.HTTPDigestAuth(
                                             self.user, self.password),
                                         verify=self.verify,
                                         stream=True)
                    else:
                        r = requests.get(_file, verify=self.verify,
                                         stream=True)

                    status = r.status_code == requests.codes.ok
                    if not status:
                        r.raise_for_status()

                    with open(f, "wb") as txz:
                        pbar = tqdm.tqdm(
                            total=int(r.headers.get('content-length')),
                            bar_format="{desc}{percentage:3.0f}%"
                                       " {rate_fmt}"
                                       " Elapsed: {elapsed}"
                                       " Remaining: {remaining}",
                            unit="bit",
                            unit_scale=True)
                        pbar.set_description(f"Downloading: {f}")

                        for chunk in r.iter_content(chunk_size=1024):
                            txz.write(chunk)
                            pbar.update(len(chunk))
                        pbar.close()
            elif ftp:
                for f in _list:
                    _ftp = self.__fetch_ftp_connect__()
                    _ftp.cwd(self.release)

                    if bool(re.compile(
                            r"MANIFEST|base.txz|lib32.txz|doc.txz").match(f)):
                        try:
                            _ftp.voidcmd('TYPE I')
                            try:
                                filesize = _ftp.size(f)
                            except ftplib.error_perm:
                                # Could be HardenedBSD on a custom FTP
                                # server, or they just don't have every
                                # file we want. The only truly important
                                # ones are base.txz and MANIFEST for us,
                                # the rest are not.
                                if f != "base.txz" and f != "MANIFEST":
                                    self.files = tuple(x for x in _list
                                                       if x != f)
                                    continue
                                else:
                                    iocage.lib.ioc_common.logit({
                                        "level"  : "EXCEPTION",
                                        "message": f"{f} is required!"
                                    }, exit_on_error=self.exit_on_error,
                                        _callback=self.callback,
                                        silent=self.silent)

                            with open(f, "wb") as txz:
                                pbar = tqdm.tqdm(total=filesize,
                                                 bar_format="{desc}{"
                                                            "percentage:3.0f}%"
                                                            " {rate_fmt}"
                                                            " Elapsed: {"
                                                            "elapsed}"
                                                            " Remaining: {"
                                                            "remaining}",
                                                 unit="bit",
                                                 unit_scale=True)
                                pbar.set_description(
                                    f"Downloading: {f}")

                                def callback(chunk):
                                    txz.write(chunk)
                                    pbar.update(len(chunk))

                                _ftp.retrbinary(f"RETR {f}", callback)
                                pbar.close()
                                _ftp.quit()
                        except:
                            raise
                    else:
                        pass

    def __fetch_check_members__(self, members):
        """Checks if the members are relative, if not, log a warning."""
        _members = []

        for m in members:
            if m.name == ".":
                continue

            if ".." in m.name or not m.name.startswith("./"):
                iocage.lib.ioc_common.logit({
                    "level"  : "WARNING",
                    "message":
                        f"{m.name} is not a relative file, skipping "
                },
                    _callback=self.callback,
                    silent=self.silent)
                continue

            _members.append(m)

        return _members

    def fetch_extract(self, f):
        """
        Takes a src and dest then creates the RELEASE dataset for the data.
        """
        src = f"{self.iocroot}/download/{self.release}/{f}"
        dest = f"{self.iocroot}/releases/{self.release}/root"

        dataset = f"{self.pool}/iocage/releases/{self.release}/root"
        if not os.path.isdir(dest):
            self.zpool.create(dataset, {
                "compression": "lz4"
            }, libzfs.DatasetType.FILESYSTEM, 0, True)

            self.zfs.get_dataset(dataset).mount_recursive(True)

        with tarfile.open(src) as f:
            # Extracting over the same files is much slower then
            # removing them first.
            member = self.__fetch_extract_remove__(f)
            member = self.__fetch_check_members__(member)
            def is_within_directory(directory, target):
                
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
            
                prefix = os.path.commonprefix([abs_directory, abs_target])
                
                return prefix == abs_directory
            
            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
            
                for member in tar.getmembers():
                    member_path = os.path.join(path, member.name)
                    if not is_within_directory(path, member_path):
                        raise Exception("Attempted Path Traversal in Tar File")
            
                tar.extractall(path, members, numeric_owner=numeric_owner) 
                
            
            safe_extract(f, dest, members=member)

    def fetch_update(self, cli=False, uuid=None):
        """This calls 'freebsd-update' to update the fetched RELEASE."""
        if cli:
            cmd = ["mount", "-t", "devfs", "devfs",
                   f"{self.iocroot}/jails/{uuid}/root/dev"]
            new_root = f"{self.iocroot}/jails/{uuid}/root"

            iocage.lib.ioc_common.logit({
                "level"  : "INFO",
                "message":
                    f"\n* Updating {uuid} to the latest patch"
                    " level... "
            },
                _callback=self.callback,
                silent=self.silent)
        else:
            cmd = ["mount", "-t", "devfs", "devfs",
                   f"{self.iocroot}/releases/{self.release}/root/dev"]
            new_root = f"{self.iocroot}/releases/{self.release}/root"

            iocage.lib.ioc_common.logit({
                "level"  : "INFO",
                "message": f"\n* Updating {self.release} to the latest patch"
                           " level... "
            },
                _callback=self.callback,
                silent=self.silent)

        su.Popen(cmd).communicate()
        shutil.copy("/etc/resolv.conf", f"{new_root}/etc/resolv.conf")

        os.environ["UNAME_r"] = self.release
        os.environ["PAGER"] = "/bin/cat"
        if os.path.isfile(f"{new_root}/etc/freebsd-update.conf"):
            f = "https://raw.githubusercontent.com/freebsd/freebsd" \
                "/master/usr.sbin/freebsd-update/freebsd-update.sh"

            tmp = tempfile.NamedTemporaryFile(delete=False)
            with urllib.request.urlopen(f) as fbsd_update:
                tmp.write(fbsd_update.read())
            tmp.close()
            os.chmod(tmp.name, 0o755)

            fetch_cmd = [tmp.name, "-b", new_root, "-d",
                         f"{new_root}/var/db/freebsd-update/",
                         "-f",
                         f"{new_root}/etc/freebsd-update.conf",
                         "--not-running-from-cron",
                         "fetch"]

            fetch = su.Popen(fetch_cmd)
            fetch.communicate()

            if not fetch.returncode:
                # They may have missing files, we don't need that noise
                # since it's not fatal
                su.Popen([tmp.name, "-b", new_root, "-d",
                          f"{new_root}/var/db/freebsd-update/",
                          "-f",
                          f"{new_root}/etc/freebsd-update.conf",
                          "install"],
                         stderr=su.DEVNULL).communicate()

            if not tmp.closed:
                tmp.close()

            os.remove(tmp.name)

        try:
            if not cli:
                # Why this sometimes doesn't exist, we may never know.
                os.remove(f"{new_root}/etc/resolv.conf")
        except OSError:
            pass

        su.Popen(["umount", f"{new_root}/dev"]).communicate()

    def fetch_plugin(self, _json, props, num, accept_license):
        """Helper to fetch plugins"""
        _json = f"{self.iocroot}/.plugin_index/{_json}.json" if not \
            _json.endswith(".json") else _json

        with open(f"{self.iocroot}/.plugin_index/INDEX", "r") as plugins:
            plugins = json.load(plugins)

        try:
            with open(_json, "r") as j:
                conf = json.load(j)
        except FileNotFoundError:
            iocage.lib.ioc_common.logit({
                "level"  : "EXCEPTION",
                "message": f"{_json} was not found!"
            }, exit_on_error=self.exit_on_error, _callback=self.callback,
                silent=self.silent)
        except json.decoder.JSONDecodeError:
            iocage.lib.ioc_common.logit({
                "level"  : "EXCEPTION",
                "message": "Invalid JSON file supplied, please supply a "
                           "correctly formatted JSON file."
            }, exit_on_error=self.exit_on_error, _callback=self.callback,
                silent=self.silent)

        if self.hardened:
            conf['release'] = conf['release'].replace("-RELEASE", "-STABLE")
            conf['release'] = re.sub(r"\W\w.", "-", conf['release'])

        self.release = conf['release']
        self.__fetch_plugin_inform__(conf, num, plugins, accept_license)
        props, pkg = self.__fetch_plugin_props__(conf, props, num)
        jail_name = conf["name"]
        location = f"{self.iocroot}/jails/{jail_name}"

        try:
            jaildir, _conf, repo_dir = self.__fetch_plugin_create__(props,
                                                                    jail_name)
            self.__fetch_plugin_install_packages__(jail_name, jaildir, conf,
                                                   _conf, pkg, props, repo_dir)
            self.__fetch_plugin_post_install__(conf, _conf, jaildir, jail_name)
        except KeyboardInterrupt:
            iocage.lib.ioc_destroy.IOCDestroy().destroy_jail(location)
            sys.exit(1)

    def __fetch_plugin_inform__(self, conf, num, plugins, accept_license):
        """Logs the pertinent information before fetching a plugin"""
        if num <= 1:
            iocage.lib.ioc_common.logit({
                "level"  : "INFO",
                "message": f"Plugin: {conf['name']}"
            },
                _callback=self.callback,
                silent=self.silent)
            iocage.lib.ioc_common.logit({
                "level"  : "INFO",
                "message": f"  Using RELEASE: {conf['release']}"
            },
                _callback=self.callback,
                silent=self.silent)
            iocage.lib.ioc_common.logit({
                "level"  : "INFO",
                "message":
                    f"  Post-install Artifact: {conf['artifact']}"
            },
                _callback=self.callback,
                silent=self.silent)
            iocage.lib.ioc_common.logit({
                "level"  : "INFO",
                "message": "  These pkgs will be installed:"
            },
                _callback=self.callback,
                silent=self.silent)

            for pkg in conf["pkgs"]:
                iocage.lib.ioc_common.logit({
                    "level"  : "INFO",
                    "message": f"    - {pkg}"
                },
                    _callback=self.callback,
                    silent=self.silent)

            # Name would be convenient, but it doesn't always gel with the
            # JSON's title, pkg always does.
            try:
                license = plugins[pkg.split("/", 1)[-1]].get("license", False)
            except UnboundLocalError:
                license = plugins[conf["name"].lower().split("/", 1)[-1]].get(
                    "license", False)
            except KeyError:
                # quassel-core is one that does this.
                name = plugins[conf["name"].strip("-").lower().split(
                    "/", 1)[-1]]
                license = name.get("license", False)

            if license and not accept_license:
                license_text = requests.get(license)

                iocage.lib.ioc_common.logit({
                    "level"  : "WARNING",
                    "message": "  This plugin requires accepting a license "
                               "to proceed:"
                },
                    _callback=self.callback,
                    silent=self.silent)
                iocage.lib.ioc_common.logit({
                    "level"  : "VERBOSE",
                    "message": f"{license_text.text}"
                },
                    _callback=self.callback,
                    silent=self.silent)

                agree = input("Do you agree? (y/N) ")

                if agree.lower() != "y":
                    iocage.lib.ioc_common.logit({
                        "level"  : "EXCEPTION",
                        "message": "You must accept the license to continue!"
                    }, exit_on_error=self.exit_on_error,
                        _callback=self.callback,
                        silent=self.silent)

    def __fetch_plugin_props__(self, conf, props, num):
        """Generates the list of properties that a user and the JSON supply"""
        self.release = conf["release"]
        pkg_repos = conf["fingerprints"]
        freebsd_version = f"{self.iocroot}/releases/{conf['release']}" \
                          "/root/bin/freebsd-version"
        json_props = conf.get("properties", {})
        props = list(props)

        for p, v in json_props.items():
            # The JSON properties are going to be treated as user entered
            # ones on the command line. If the users prop exists on the
            # command line, we will skip the JSON one.
            _p = f"{p}={v}"

            if p not in [_prop.split("=")[0] for _prop in props]:
                props.append(_p)

            if not os.path.isdir(f"{self.iocroot}/releases/{self.release}"):
                self.fetch_release()

        if conf["release"][:4].endswith("-"):
            # 9.3-RELEASE and under don't actually have this binary.
            release = conf["release"]
        else:
            try:
                with open(freebsd_version, "r") as r:
                    for line in r:
                        if line.startswith("USERLAND_VERSION"):
                            release = line.rstrip().partition("=")[2].strip(
                                '"')
            except FileNotFoundError:
                iocage.lib.ioc_common.logit({
                    "level"  : "WARNING",
                    "message": f"Release {self.release} missing, "
                               f"will attempt to fetch it."
                },
                    _callback=self.callback,
                    silent=self.silent)

                self.fetch_release()

                # We still want this.
                with open(freebsd_version, "r") as r:
                    for line in r:
                        if line.startswith("USERLAND_VERSION"):
                            release = line.rstrip().partition("=")[2].strip(
                                '"')

        # We set our properties that we need, and then iterate over the user
        # supplied properties replacing ours.
        create_props = [f"cloned_release={self.release}",
                        f"release={release}", "type=plugin", "boot=on"]

        create_props = [f"{k}={v}" for k, v in
                        (p.split("=") for p in props)] + create_props

        return create_props, pkg_repos

    def __fetch_plugin_create__(self, create_props, uuid):
        """Creates the plugin with the provided properties"""
        iocage.lib.ioc_create.IOCCreate(
            self.release, create_props, 0, silent=True, uuid=uuid,
            exit_on_error=self.exit_on_error).create_jail()
        jaildir = f"{self.iocroot}/jails/{uuid}"
        repo_dir = f"{jaildir}/root/usr/local/etc/pkg/repos"
        path = f"{self.pool}/iocage/jails/{uuid}"
        _conf = iocage.lib.ioc_json.IOCJson(jaildir).json_load()

        # We do this test again as the user could supply a malformed IP to
        # fetch that bypasses the more naive check in cli/fetch
        if _conf["ip4_addr"] == "none" and _conf["ip6_addr"] == "none" and \
           _conf["dhcp"] != "on":
            iocage.lib.ioc_common.logit({
                "level"  : "ERROR",
                "message": "\nAn IP address is needed to fetch a "
                           "plugin!\n"
            },
                _callback=self.callback,
                silent=self.silent)
            iocage.lib.ioc_destroy.IOCDestroy().destroy_jail(path)
            iocage.lib.ioc_common.logit({
                "level"  : "EXCEPTION",
                "message": "Destroyed partial plugin."
            }, exit_on_error=self.exit_on_error,
                _callback=self.callback,
                silent=self.silent)

        return jaildir, _conf, repo_dir

    def __fetch_plugin_install_packages__(self, uuid, jaildir, conf,
                                          _conf, pkg_repos, create_props,
                                          repo_dir):
        """Attempts to start the jail and install the packages"""
        kmods = conf.get("kmods", {})

        for kmod in kmods:
            try:
                su.check_call(["kldload", "-n", kmod], stdout=su.PIPE,
                              stderr=su.PIPE)
            except su.CalledProcessError:
                iocage.lib.ioc_common.logit({
                    "level"  : "EXCEPTION",
                    "message": "Module not found!"
                }, exit_on_error=self.exit_on_error, _callback=self.callback,
                    silent=self.silent)

        try:
            os.makedirs(f"{jaildir}/root/usr/local/etc/pkg/repos", 0o755)
        except OSError:
            # Same as below, it exists and we're OK with that.
            pass

        freebsd_conf = """\
FreeBSD: { enabled: no }
"""

        try:
            os.makedirs(repo_dir, 0o755)
        except OSError:
            # It exists, that's fine.
            pass

        with open(f"{jaildir}/root/usr/local/etc/pkg/repos/FreeBSD.conf",
                  "w") as f_conf:
            f_conf.write(freebsd_conf)

        for repo in pkg_repos:
            repo_name = repo
            repo = pkg_repos[repo]
            f_dir = f"{jaildir}/root/usr/local/etc/pkg/fingerprints/" \
                    f"{repo_name}/trusted"
            repo_conf = """\
{reponame}: {{
            url: "{packagesite}",
            signature_type: "fingerprints",
            fingerprints: "/usr/local/etc/pkg/fingerprints/{reponame}",
            enabled: true
            }}
"""

            try:
                os.makedirs(f_dir, 0o755)
            except OSError:
                iocage.lib.ioc_common.logit({
                    "level"  : "ERROR",
                    "message": f"Repo: {repo_name} already exists, skipping!"
                },
                    _callback=self.callback,
                    silent=self.silent)

            r_file = f"{repo_dir}/{repo_name}.conf"

            with open(r_file, "w") as r_conf:
                r_conf.write(repo_conf.format(reponame=repo_name,
                                              packagesite=conf["packagesite"]))

            f_file = f"{f_dir}/{repo_name}"

            for r in repo:
                finger_conf = """\
function: {function}
fingerprint: {fingerprint}
"""
                with open(f_file, "w") as f_conf:
                    f_conf.write(finger_conf.format(function=r["function"],
                                                    fingerprint=r[
                                                        "fingerprint"]))
        err = iocage.lib.ioc_create.IOCCreate(
            self.release, create_props, 0, pkglist=conf["pkgs"], silent=True,
            exit_on_error=self.exit_on_error).create_install_packages(
            uuid, jaildir, _conf, repo=conf["packagesite"], site=repo_name)

        if err:
            iocage.lib.ioc_common.logit({
                "level"  : "ERROR",
                "message": "pkg error, refusing to fetch artifact and "
                           "run post_install.sh!\n"
            },
                _callback=self.callback,
                silent=self.silent)

    def __fetch_plugin_post_install__(self, conf, _conf, jaildir, uuid):
        """Fetches the users artifact and runs the post install"""
        dhcp = False

        try:
            ip4 = _conf["ip4_addr"].split("|")[1].rsplit(
                "/")[0]
        except IndexError:
            ip4 = "none"

        try:
            ip6 = _conf["ip6_addr"].split("|")[1].rsplit(
                "/")[0]
        except IndexError:
            ip6 = "none"

        if ip4 != "none":
            ip = ip4
        elif ip6 != "none":
            # If they had an IP4 address and an IP6 one,
            # we'll assume they prefer IP6.
            ip = ip6
        else:
            dhcp = True
            ip = ""

        os.environ["IOCAGE_PLUGIN_IP"] = ip

        # We need to pipe from tar to the root of the jail.
        if conf["artifact"]:
            iocage.lib.ioc_common.logit({
                "level"  : "INFO",
                "message": "Fetching artifact... "
            },
                _callback=self.callback,
                silent=self.silent)

            pygit2.clone_repository(conf["artifact"], f"{jaildir}/plugin",
                                    checkout_branch='master')
            try:
                distutils.dir_util.copy_tree(f"{jaildir}/plugin/overlay/",
                                             f"{jaildir}/root",
                                             preserve_symlinks=True)
            except distutils.errors.DistutilsFileError:
                # It just doesn't exist
                pass

            try:
                shutil.copy(f"{jaildir}/plugin/post_install.sh",
                            f"{jaildir}/root/root")

                iocage.lib.ioc_common.logit({
                    "level"  : "INFO",
                    "message": "Running post_install.sh"
                },
                    _callback=self.callback,
                    silent=self.silent)

                command = ["sh", "/root/post_install.sh"]
                msg, err = iocage.lib.ioc_exec.IOCExec(command, uuid, jaildir,
                                                       plugin=True,
                                                       skip=True).exec_jail()
                if err:
                    iocage.lib.ioc_common.logit({
                        "level": "EXCEPTION",
                        "message": "An error occured! Please read above"
                    },
                        exit_on_error=self.exit_on_error
                    )

                iocage.lib.ioc_common.logit({
                    "level"  : "INFO",
                    "message": msg
                })

                ui_json = f"{jaildir}/plugin/ui.json"

                if dhcp:
                    interface = _conf["interfaces"].split(",")[0].split(":")[0]
                    ip4_cmd = ["jexec", f"ioc-{uuid}", "ifconfig", interface,
                               "inet"]
                    out = su.check_output(ip4_cmd).decode()
                    ip = f"{out.splitlines()[2].split()[1]}"
                    os.environ["IOCAGE_PLUGIN_IP"] = ip

                try:
                    with open(ui_json, "r") as u:
                        admin_portal = json.load(u)["adminportal"]
                        admin_portal = admin_portal.replace("%%IP%%", ip)
                        iocage.lib.ioc_common.logit({
                            "level"  : "INFO",
                            "message": f"Admin Portal:\n{admin_portal}"
                        },
                            _callback=self.callback,
                            silent=self.silent)
                except FileNotFoundError:
                    # They just didn't set a admin portal.
                    pass
            except FileNotFoundError:
                pass

    def fetch_plugin_index(self, props, _list=False, list_header=False,
                           list_long=False, accept_license=False):
        if self.server == "ftp.freebsd.org":
            git_server = "https://github.com/freenas/iocage-ix-plugins.git"
        else:
            git_server = self.server

        git_working_dir = f"{self.iocroot}/.plugin_index"

        # list --plugins won't often be root.
        if os.geteuid() == 0:
            try:
                pygit2.clone_repository(git_server, git_working_dir)
            except pygit2.GitError as err:
                iocage.lib.ioc_common.logit({
                    "level"  : "EXCEPTION",
                    "message": err
                }, exit_on_error=self.exit_on_error,
                    _callback=self.callback,
                    silent=self.silent)
            except ValueError:
                try:
                    repo = pygit2.Repository(git_working_dir)
                    iocage.lib.ioc_common.git_pull(
                        repo, exit_on_error=self.exit_on_error)
                except (pygit2.GitError, AssertionError, RuntimeError) as err:
                    iocage.lib.ioc_common.logit({
                        "level"  : "EXCEPTION",
                        "message": err
                    }, exit_on_error=self.exit_on_error,
                        _callback=self.callback,
                        silent=self.silent)

        with open(f"{self.iocroot}/.plugin_index/INDEX", "r") as plugins:
            plugins = json.load(plugins)

        _plugins = self.__fetch_sort_plugin__(plugins)
        if self.plugin is None and not _list:
            for p in _plugins:
                iocage.lib.ioc_common.logit({
                    "level"  : "INFO",
                    "message": f"[{_plugins.index(p)}] {p}"
                },
                    _callback=self.callback,
                    silent=self.silent)

        if _list:
            plugin_list = []

            for p in _plugins:
                p = p.split("-", 1)
                name = p[0]
                desc, pkg = re.sub(r'[()]', '', p[1]).rsplit(" ", 1)
                license = plugins[pkg].get("license", "")

                p = [name, desc, pkg]

                if not list_header:
                    p += [license]

                plugin_list.append(p)

            if not list_header:
                return plugin_list
            else:
                if list_long:
                    table = texttable.Texttable(max_width=0)
                else:
                    table = texttable.Texttable(max_width=80)

                # We get an infinite float otherwise.
                table.set_cols_dtype(["t", "t", "t"])
                plugin_list.insert(0, ["NAME", "DESCRIPTION", "PKG"])

                table.add_rows(plugin_list)

                return table.draw()

        if self.plugin is None:
            self.plugin = input("\nType the number of the desired"
                                " plugin\nPress [Enter] or type EXIT to"
                                " quit: ")

        self.plugin = self.__fetch_validate_plugin__(self.plugin.lower(),
                                                     _plugins)
        self.fetch_plugin(f"{self.iocroot}/.plugin_index/{self.plugin}.json",
                          props, 0, accept_license)

    def __fetch_validate_plugin__(self, plugin, plugins):
        """
        Checks if the user supplied an index number and returns the
        plugin. If they gave us a plugin name, we make sure that exists in
        the list at all.
        """
        _plugin = plugin  # Gets lost in the enumeration if no match is found.
        if plugin.lower() == "exit":
            exit()

        if len(plugin) <= 2:
            try:
                plugin = plugins[int(plugin)]
            except IndexError:
                iocage.lib.ioc_common.logit({
                    "level"  : "EXCEPTION",
                    "message": f"Plugin: {_plugin} not in list!"
                }, exit_on_error=self.exit_on_error,
                    _callback=self.callback,
                    silent=self.silent)
            except ValueError:
                exit()
        else:
            # Quick list validation
            try:
                plugin = [i for i, p in enumerate(plugins) if
                          plugin.capitalize() in p or plugin in p]
                try:
                    plugin = plugins[int(plugin[0])]
                except IndexError:
                    iocage.lib.ioc_common.logit({
                        "level"  : "EXCEPTION",
                        "message": f"Plugin: {_plugin} not in list!"
                    }, exit_on_error=self.exit_on_error,
                        _callback=self.callback,
                        silent=self.silent)
            except ValueError as err:
                iocage.lib.ioc_common.logit({
                    "level"  : "EXCEPTION",
                    "message": err
                }, exit_on_error=self.exit_on_error,
                    _callback=self.callback,
                    silent=self.silent)

        return plugin.rsplit("(", 1)[1].replace(")", "")

    def __fetch_sort_plugin__(self, plugins):
        """
        Sort the list by plugin.
        """
        p_dict = {}
        plugin_list = []

        for plugin in plugins:
            _plugin = f"{plugins[plugin]['name']} -" \
                      f" {plugins[plugin]['description']}" \
                      f" ({plugin})"
            p_dict[plugin] = _plugin

        ordered_p_dict = collections.OrderedDict(sorted(p_dict.items()))
        index = 0

        for p in ordered_p_dict.values():
            plugin_list.insert(index, f"{p}")
            index += 1

        return plugin_list

    def __fetch_extract_remove__(self, tar):
        """
        Tries to remove any file that exists from the archive as overwriting
        is very slow in tar.
        """
        members = []

        for f in tar.getmembers():
            rel_path = f"{self.iocroot}/releases/{self.release}/root/" \
                       f"{f.name}"
            try:
                # . and so forth won't like this.
                os.remove(rel_path)
            except (IOError, OSError):
                pass

            members.append(f)

        return members
