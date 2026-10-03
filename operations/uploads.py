from django.core.files.uploadhandler import TemporaryFileUploadHandler, StopUpload
class BatchUploadHandler(TemporaryFileUploadHandler):
    """限制流式上传，不读取任意本地路径。 / Bound streamed uploads without reading arbitrary local paths."""
    def handle_raw_input(self,*args,**kwargs):
        self.total=0
        self.count=0
        self.current=0
    def new_file(self,*args,**kwargs):
        self.count+=1
        self.current=0
        if self.count>50:
            self.request.upload_failure="每批最多 50 个文件。"
            raise StopUpload(connection_reset=False)
        super().new_file(*args,**kwargs)
    def receive_data_chunk(self,raw_data,start):
        self.current+=len(raw_data)
        self.total+=len(raw_data)
        if self.current>25*1024*1024 or self.total>200*1024*1024:
            self.request.upload_failure="单文件不超过 25MB，每批不超过 200MB。"
            self.file.close()
            raise StopUpload(connection_reset=False)
        return super().receive_data_chunk(raw_data,start)

