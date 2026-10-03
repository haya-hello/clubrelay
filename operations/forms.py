import uuid
from django import forms
from activities.models import Activity
from .models import Person, Evidence, ModelConfiguration

class EventChoice(forms.ModelChoiceField):
    def label_from_instance(self,obj):
        return obj.title

class FilterForm(forms.Form):
    start=forms.DateField(label="开始日期",required=False,widget=forms.DateInput(attrs={"type":"date"},format="%Y-%m-%d"))
    end=forms.DateField(label="结束日期",required=False,widget=forms.DateInput(attrs={"type":"date"},format="%Y-%m-%d"))
    def clean(self):
        data=super().clean()
        if data.get("start") and data.get("end") and data["start"]>data["end"]:
            raise forms.ValidationError("开始日期不能晚于结束日期。")
        return data

class UploadForm(forms.Form):
    token=forms.UUIDField(initial=uuid.uuid4,widget=forms.HiddenInput)
    event=EventChoice(label="归入已有活动",queryset=Activity.objects.all(),required=False,empty_label="新建活动档案")
    title=forms.CharField(label="新活动名称",max_length=120,required=False,help_text="不确定可留空，先作为待归档批次保存。")
    recorded_date=forms.DateField(label="活动发生日期",required=False,widget=forms.DateInput(attrs={"type":"date"},format="%Y-%m-%d"),help_text="不知道就留空，不用上传日期代替。")

class EventForm(forms.ModelForm):
    class Meta:
        model=Activity
        fields=["title","recorded_date","event_status","description"]
        labels={"title":"活动名称","recorded_date":"发生日期","event_status":"活动性质","description":"简要说明"}
        widgets={"recorded_date":forms.DateInput(attrs={"type":"date"},format="%Y-%m-%d"),"description":forms.Textarea(attrs={"rows":3})}

class PersonForm(forms.ModelForm):
    aliases=forms.CharField(label="别名/群昵称",required=False,max_length=1000,help_text="多个名称用逗号分开；重名不会自动合并。")
    class Meta:
        model=Person
        fields=["external_id","display_name","role","interests","joined_on","notes","active"]
        labels={"external_id":"稳定成员编号","display_name":"姓名/显示名","role":"已知角色","interests":"兴趣/能力方向（负责人记录）","joined_on":"加入日期","notes":"负责人补充","active":"当前在册"}
        widgets={"joined_on":forms.DateInput(attrs={"type":"date"},format="%Y-%m-%d"),"notes":forms.Textarea(attrs={"rows":3})}

class EvidenceForm(forms.ModelForm):
    class Meta:
        model=Evidence
        fields=["member","event","title","kind","confidence","occurred_on","material","anchor","quote","note"]
        labels={"member":"对应成员","event":"活动","title":"事项或作品","kind":"证据维度","confidence":"证据状态","occurred_on":"发生日期","material":"来源资料","anchor":"原文位置","quote":"原文摘录或线下依据","note":"负责人说明"}
        widgets={"occurred_on":forms.DateInput(attrs={"type":"date"},format="%Y-%m-%d"),"quote":forms.Textarea(attrs={"rows":4}),"note":forms.Textarea(attrs={"rows":2})}
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields["member"].label_from_instance=lambda obj:f"{obj.display_name}（{obj.external_id}）"
        self.fields["event"].label_from_instance=lambda obj:obj.title
        self.fields["material"].queryset=self.fields["material"].queryset.filter(excluded=False,parse_status="parsed")
        self.fields["material"].label_from_instance=lambda obj:obj.original_name
    def clean(self):
        data=super().clean()
        mat=data.get("material")
        if mat:
            if data.get("event") and data["event"].pk!=mat.event_id:
                self.add_error("material","来源资料不属于该活动。")
            if data.get("quote") and data["quote"] not in mat.text:
                self.add_error("quote","引用必须逐字存在于所选资料中。")
        return data

class ConfigurationForm(forms.ModelForm):
    expected_fingerprint=forms.CharField(max_length=64,widget=forms.HiddenInput)
    api_key=forms.CharField(label="API Key（不修改可留空）",required=False,max_length=1000,widget=forms.PasswordInput(render_value=False))
    acknowledgement=forms.BooleanField(label="我确认此模型的处理位置；只有另行授权的活动资料才允许发送",required=False)
    class Meta:
        model=ModelConfiguration
        fields=["mode","base_url","model","enabled","local_auth"]
        labels={"mode":"处理位置","base_url":"模型接口地址","model":"实际模型标识","enabled":"启用模型分析","local_auth":"向本机接口发送已保存的 API Key（仅当此接口需要凭据时勾选）"}
        help_texts={"local_auth":"默认不向本机接口发送密钥；接口在本机不代表推理一定在本机。"}
    def clean(self):
        data=super().clean()
        if data.get("enabled"):
            if data.get("mode")=="disabled" or not data.get("base_url") or not data.get("model"):
                raise forms.ValidationError("启用前请填写处理位置、接口地址和实际模型标识。")
            if not data.get("acknowledgement"):
                self.add_error("acknowledgement","启用需明确确认处理位置。")
            if data.get("base_url") and data.get("mode") in ("local","cloud"):
                from .ai_client import _endpoint, AIError
                try:
                    _endpoint(data["base_url"],data["mode"])
                except AIError as exc:
                    self.add_error("base_url",exc.message)
        return data

class AskForm(FilterForm):
    event=EventChoice(label="分析范围",queryset=Activity.objects.all(),required=False,empty_label="所有已授权活动")
    member=forms.ModelChoiceField(label="限定成员",queryset=Person.objects.all(),required=False,empty_label="不限定")
    question=forms.CharField(label="你的问题",max_length=1000,widget=forms.Textarea(attrs={"rows":3}))
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields["member"].label_from_instance=lambda obj:f"{obj.display_name}（{obj.external_id}）"
