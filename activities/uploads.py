from django.core.files.uploadhandler import MemoryFileUploadHandler, StopUpload

class BoundedMemoryUploadHandler(MemoryFileUploadHandler):
    """边读边限制单附件大小，避免落入系统临时目录。 / Bound one attachment while reading; avoid system temp files."""
    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        self.activated = True
        self.total = 0
        self.files_seen = 0

    def new_file(self, *args, **kwargs):
        self.files_seen += 1
        if self.files_seen > 1:
            self.request.upload_failure = "每次提交只允许一个附件。"
            raise StopUpload(connection_reset=False)
        super().new_file(*args, **kwargs)

    def receive_data_chunk(self, raw_data, start):
        self.total += len(raw_data)
        if self.total > 5 * 1024 * 1024:
            self.request.upload_failure = "附件不超过 5MB；本次未保存任何成果。"
            self.file.close()
            raise StopUpload(connection_reset=False)
        return super().receive_data_chunk(raw_data, start)
