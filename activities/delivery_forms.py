import uuid
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from django import forms

ALLOWED_FILES = {".txt",".md",".pdf",".png",".jpg",".jpeg",".html",".zip"}
MAX_FILE_SIZE = 5 * 1024 * 1024

def validate_file(upload):
    if upload:
        suffix = PurePosixPath(upload.name.replace("\\", "/")).suffix.lower()
        if suffix not in ALLOWED_FILES:
            raise forms.ValidationError("仅支持 TXT、MD、PDF、PNG、JPG、HTML、ZIP 附件。")
        if not 0 < upload.size <= MAX_FILE_SIZE:
            raise forms.ValidationError("附件须为非空文件，且不超过 5MB。")
    return upload

class SubmissionForm(forms.Form):
    token = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    summary = forms.CharField(label="成果说明", max_length=3000, widget=forms.Textarea(attrs={"rows":3}))
    result_text = forms.CharField(label="成果正文", required=False, max_length=20000, widget=forms.Textarea(attrs={"rows":5}), help_text="正文、链接、附件至少提供一种；不是只写完成声明。")
    result_url = forms.URLField(label="成果链接", required=False, max_length=2000)
    attachment = forms.FileField(label="成果附件", required=False, validators=[validate_file], help_text="单文件不超过 5MB；只保存和下载，不执行、解压或在线解析。")
    method = forms.CharField(label="方法与验证过程", max_length=3000, widget=forms.Textarea(attrs={"rows":3}), help_text="说明如何使用 AI、自己修改了什么，以及怎样验证结果。")
    contribution = forms.CharField(label="本人实际分工", max_length=3000, widget=forms.Textarea(attrs={"rows":3}), help_text="只记录自己的工作；多人协作请分别承担任务，不能代领全队贡献。")

    def clean(self):
        data = super().clean()
        if not any(data.get(key) for key in ["result_text","result_url","attachment"]):
            raise forms.ValidationError("请提供成果正文、链接或附件中的至少一种。")
        url = data.get("result_url")
        if url and (urlsplit(url).scheme not in ("http","https") or urlsplit(url).username or urlsplit(url).password):
            self.add_error("result_url","仅允许不含账号密码的 HTTP/HTTPS 链接。")
        return data

class AcceptanceForm(forms.Form):
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    action = forms.ChoiceField(choices=[("approve","通过"),("return","退回"),("revoke","撤销")], widget=forms.HiddenInput)
    reason = forms.CharField(label="验收意见", max_length=1000, widget=forms.Textarea(attrs={"rows":3}))
    practice = forms.BooleanField(label="确认 AI 实践", required=False)
    contribution = forms.BooleanField(label="确认社团贡献", required=False)

    def clean(self):
        data = super().clean()
        if data.get("action") == "approve" and not (data.get("practice") or data.get("contribution")):
            raise forms.ValidationError("验收通过时请至少确认 AI 实践或社团贡献中的一项。")
        return data

