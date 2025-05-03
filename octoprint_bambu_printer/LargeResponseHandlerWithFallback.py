from octoprint.server.util.tornado import LargeResponseHandler
import os


class LargeResponseHandlerWithFallback(LargeResponseHandler):
    def parse_url_path(self, url_path: str) -> str:
        abs_path = os.path.join(self.root, url_path)
        if not os.path.exists(abs_path):
            abs_path = os.path.join(self.root, self.default_filename)
        return abs_path

