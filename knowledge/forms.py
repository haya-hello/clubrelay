import uuid
from django import forms
from .models import Entry

class EntryForm(forms.ModelForm):
    token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    version = forms.IntegerField(widget=forms.HiddenInput, required=False, min_value=1)

    class Meta:
        model = Entry
        fields = ["title", "category", "body", "source", "applicability"]
        labels = {"title": "经验标题", "category": "知识分类", "body": "经验正文", "source": "来源说明", "applicability": "适用场景与限制"}
        widgets = {"body": forms.Textarea(attrs={"rows": 12, "placeholder": "发生了什么？遇到什么问题？如何处理？结果怎样？下次需要注意什么？", "maxlength": 50000}), "applicability": forms.Textarea(attrs={"rows": 2}), "title": forms.TextInput(attrs={"placeholder": "例如：活动前设备检查清单"}), "source": forms.TextInput(attrs={"placeholder": "例如：虚构工作坊筹备经验，仅用于演示"})}

    def clean_body(self):
        body = self.cleaned_data["body"].strip()
        if not body or len(body) > 50000:
            raise forms.ValidationError("正文需为 1 至 50,000 个字符。")
        return body

class ReviewForm(forms.Form):
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    action = forms.ChoiceField(choices=[("approve", "通过"), ("reject", "退回"), ("withdraw", "下架")], widget=forms.HiddenInput)
    reason = forms.CharField(label="审核意见", max_length=1000, widget=forms.Textarea(attrs={"rows": 3, "placeholder": "请说明通过依据或需要修改的问题。"}))

class SearchForm(forms.Form):
    q = forms.CharField(required=False, max_length=120, label="关键词")
    category = forms.ChoiceField(required=False, choices=[("", "全部分类"), *Entry.Category.choices])

class ApprovedEntryChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.title} · v{obj.version}"

class QuestionForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .services import searchable_entries
        self.fields['scope'].queryset = searchable_entries()

    token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    question = forms.CharField(label="描述你的问题", max_length=600, widget=forms.Textarea(attrs={"rows": 3, "placeholder": "例如：活动前要检查哪些设备？"}))
    scope = ApprovedEntryChoice(label="检索范围", queryset=Entry.objects.filter(status=Entry.Status.APPROVED), required=False, empty_label="全部已审核经验")
    use_previous = forms.BooleanField(label="沿用上一问主题", required=False)
