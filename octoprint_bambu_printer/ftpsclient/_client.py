"""wrapper for FTPS server interactions"""

import ftplib
import ssl
from typing import List, Optional, Union


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
            conn = self.context.wrap_socket(conn,
                                            server_hostname=self.host,
                                            session=self.sock.session)  # this is the fix
        return conn, size


class IoTFTPSClient:
    """iot ftps ftpsclient"""

    ftps_host: str
    ftps_port: int
    ftps_user: str
    ftps_pass: str
    ssl_implicit: bool
    ftps_session: Union[ftplib.FTP, ImplicitTLS]

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
        try:
            if self.ssl_implicit:
                self.ftps_session = ImplicitTLS()
            else:
                self.ftps_session = ftplib.FTP()

            self.ftps_session.connect(host=self.ftps_host, port=self.ftps_port)

            if self.ftps_user != "" and self.ftps_pass != "":
                self.ftps_session.login(user=self.ftps_user, passwd=self.ftps_pass)
            else:
                self.ftps_session.login()

            if self.ssl_implicit:
                self.ftps_session.prot_p()

        except Exception as ex:
            print(f"unexpected exception occurred: {ex}")
            pass
        return

    def disconnect(self) -> None:
        """disconnect the current session from the ftps server"""
        try:
            self.ftps_session.close()
        except Exception as ex:
            print(f"unexpected exception occurred: {ex}")
            pass
        return

    def download_file(self, source: str, dest: str) -> bool:
        """download a file to a path on the local filesystem"""
        try:
            with open(dest, "wb") as file:
                self.ftps_session.retrbinary(f"RETR {source}", file.write)
            return True
        except Exception as ex:
            print(f"unexpected exception occurred: {ex}")
            pass
        return False

    def upload_file(self, source: str, dest: str) -> bool:
        """upload a file to a path inside the FTPS server"""
        try:
            with open(source, "rb") as file:
                self.ftps_session.storbinary(f"STOR {dest}", file)
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

    def move_file(self, source: str, dest: str) -> bool:
        """move a file inside the FTPS server to another path inside the FTPS server"""
        try:
            self.ftps_session.rename(source, dest)
            return True
        except Exception as ex:
            print(f"unexpected exception occurred: {ex}")
            pass
        return False

    def list_files(
        self, path: str, file_pattern: Optional[str] = None
    ) -> Union[List[str], None]:
        """list files under a path inside the FTPS server"""
        try:
            files = self.ftps_session.nlst(path)
            if not files:
                return
            if file_pattern:
                return [f for f in files if file_pattern in f]
            return files
        except Exception as ex:
            print(f"unexpected exception occurred: {ex}")
            pass
        return
