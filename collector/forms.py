from django import forms

from .models import ChatSource, KnowledgeCard


class ChatSourceForm(forms.ModelForm):
    manager_ids_text = forms.CharField(
        label="负责人 QQ 开放标识",
        help_text="多个标识用逗号分隔。只有这些成员回复并发送收录指令才生效。",
    )

    class Meta:
        model = ChatSource
        fields = [
            "platform",
            "external_id",
            "display_name",
            "capture_phrase",
            "retention_days",
            "consent_confirmed",
            "enabled",
        ]
        labels = {
            "platform": "平台",
            "external_id": "群标识",
            "display_name": "群名称",
            "capture_phrase": "收录指令",
            "retention_days": "原始消息保留天数",
            "consent_confirmed": "已确认群成员知情",
            "enabled": "启用采集",
        }
        help_texts = {
            "external_id": "在目标群发送 /青链接入信息，直接复制机器人返回的群标识。",
            "retention_days": "默认 90 天，正式知识卡不随原始消息自动删除。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["manager_ids_text"].initial = ", ".join(
                self.instance.manager_ids or []
            )

    def clean_manager_ids_text(self):
        values = [
            item.strip()
            for item in self.cleaned_data["manager_ids_text"].replace("，", ",").split(",")
            if item.strip()
        ]
        if not values:
            raise forms.ValidationError("至少填写一个负责人标识。")
        return list(dict.fromkeys(values))

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.manager_ids = self.cleaned_data["manager_ids_text"]
        if commit:
            instance.save()
        return instance


class SimulationForm(forms.Form):
    source = forms.ModelChoiceField(label="授权群", queryset=ChatSource.objects.none())
    sender_id = forms.CharField(label="发送者标识", max_length=160)
    sender_name = forms.CharField(label="发送者昵称", max_length=120)
    text = forms.CharField(label="消息内容", widget=forms.Textarea(attrs={"rows": 4}))
    reply_to_id = forms.CharField(label="回复的消息 ID", required=False, max_length=180)
    message_id = forms.CharField(label="消息 ID", required=False, max_length=180)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].queryset = ChatSource.objects.filter(enabled=True)


class KnowledgeCardForm(forms.ModelForm):
    class Meta:
        model = KnowledgeCard
        fields = [
            "title",
            "problem",
            "background",
            "solution",
            "result",
            "pitfalls",
            "conditions",
        ]
        labels = {
            "title": "标题",
            "problem": "问题",
            "background": "背景",
            "solution": "解决办法",
            "result": "结果",
            "pitfalls": "踩坑",
            "conditions": "适用条件",
        }
        widgets = {
            name: forms.Textarea(attrs={"rows": 4})
            for name in ["problem", "background", "solution", "result", "pitfalls", "conditions"]
        }
