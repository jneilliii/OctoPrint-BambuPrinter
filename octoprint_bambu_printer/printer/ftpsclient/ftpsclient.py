"""
Based on: <https://github.com/dgonzo27/py-iot-utils>

MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

wrapper for FTPS server interactions
"""

from __future__ import annotations
import ftplib
import os
from pathlib import Path
import socket
import ssl
from typing import Optional, Union, List

from contextlib import redirect_stdout
import io
import re


class ImplicitTLS(ftplib.FTP_TLS):
    """ftplib.FTP_TLS sub-class to support implicit SSL FTPS"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        """return socket"""
        return self._sock

    @sock.setter
    def sock(self, value):
        """wrap and set SSL socket"""
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)

        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session
            )  # this is the fix
        return conn, size


class IoTFTPSClient:
    """iot ftps ftpsclient"""

    ftps_host: str
    ftps_port: int
    ftps_user: str
    ftps_pass: str
    ssl_implicit: bool
    ftps_session: Union[ftplib.FTP, ImplicitTLS]
    last_error: Optional[str] = None
    welcome: str

    def __init__(
        self,
        ftps_host: str,
        ftps_port: Optional[int] = 21,
        ftps_user: Optional[str] = "",
        ftps_pass: Optional[str] = "",
        ssl_implicit: Optional[bool] = False,
    ) -> None:
        self.ftps_host = ftps_host
        self.ftps_port = ftps_port
        self.ftps_user = ftps_user
        self.ftps_pass = ftps_pass
        self.ssl_implicit = ssl_implicit
        self.instantiate_ftps_session()

    def __repr__(self) -> str:
        return (
            "IoT FTPS Client\n"
            "--------------------\n"
            f"host: {self.ftps_host}\n"
            f"port: {self.ftps_port}\n"
            f"user: {self.ftps_user}\n"
            f"ssl: {self.ssl_implicit}"
        )

    def instantiate_ftps_session(self) -> None:
        """init ftps_session based on input params"""
        self.ftps_session = ImplicitTLS() if self.ssl_implicit else ftplib.FTP()
        self.ftps_session.set_debuglevel(0)

        self.welcome = self.ftps_session.connect(
            host=self.ftps_host, port=self.ftps_port
        )

        if self.ftps_user and self.ftps_pass:
            self.ftps_session.login(user=self.ftps_user, passwd=self.ftps_pass)
        else:
            self.ftps_session.login()

        if self.ssl_implicit:
            self.ftps_session.prot_p()

    def disconnect(self) -> None:
        """disconnect the current session from the ftps server"""
        self.ftps_session.close()

    def download_file(self, source: str, dest: str):
        """download a file to a path on the local filesystem"""
        with open(dest, "wb") as file:
            self.ftps_session.retrbinary(f"RETR {source}", file.write)

    def upload_file(self, source: str, dest: str, callback=None) -> bool:
        """upload a file to a path inside the FTPS server"""

        file_size = os.path.getsize(source)

        block_size = max(file_size // 100, 8192)
        rest = None

        try:
            # Taken from ftplib.storbinary but with custom ssl handling
            # due to the shitty bambu p1p ftps server TODO fix properly.
            with open(source, "rb") as fp:
                self.ftps_session.voidcmd("TYPE I")

                with self.ftps_session.transfercmd(f"STOR {dest}", rest) as conn:
                    while 1:
                        buf = fp.read(block_size)

                        if not buf:
                            break

                        conn.sendall(buf)

                        if callback:
                            callback(buf)

                    # shutdown ssl layer
                    if ftplib._SSLSocket is not None and isinstance(
                        conn, ftplib._SSLSocket
                    ):
                        # Yeah this is suposed to be conn.unwrap
                        # But since we operate in prot p mode
                        # we can close the connection always.
                        # This is cursed but it works.
                        if "vsFTPd" in self.welcome:
                            conn.unwrap()
                        else:
                            conn.shutdown(socket.SHUT_RDWR)

                return True
        except Exception as ex:
            print(f"unexpected exception occurred: {ex}")
            pass
        return False

    def delete_file(self, path: str) -> bool:
        """delete a file from under a path inside the FTPS server"""
        try:
            self.ftps_session.delete(path)
            return True
        except Exception as ex:
            print(f"unexpected exception occurred: {ex}")
            pass
        return False

    def move_file(self, source: str, dest: str):
        """move a file inside the FTPS server to another path inside the FTPS server"""
        self.ftps_session.rename(source, dest)

    def mkdir(self, path: str) -> str:
        return self.ftps_session.mkd(path)

    def list_files(self, list_path: str, extensions: str | list[str] | None = None):
        """list files under a path inside the FTPS server"""

        if extensions is None:
            _extension_acceptable = lambda p: True
        else:
            if isinstance(extensions, str):
                extensions = [extensions]
            _extension_acceptable = lambda p: any(s in p.suffixes for s in extensions)

        try:
            list_result = self.ftps_session.nlst(list_path) or []
            for file_name in list_result:
                path = Path(list_path) / file_name
                if _extension_acceptable(path):
                    yield path
        except Exception as ex:
            print(f"unexpected exception occurred: {ex}")

    def list_files_ex(self, path: str) -> Union[list[str], None]:
        """list files under a path inside the FTPS server"""
        try:
            f = io.StringIO()
            with redirect_stdout(f):
                self.ftps_session.dir(path)
            s = f.getvalue()
            files = []
            for row in s.split("\n"):
                if len(row) <= 0:
                    continue

                attribs = row.split(" ")

                match = re.search(r".*\ (\d\d\:\d\d|\d\d\d\d)\ (.*)", row)
                name = ""
                if match:
                    name = match.groups(1)[1]
                else:
                    name = attribs[len(attribs) - 1]

                file = (attribs[0], name)
                files.append(file)
            return files
        except Exception as ex:
            print(f"unexpected exception occurred: [{ex}]")
            pass
        return
