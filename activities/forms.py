import uuid
from django import forms
from django.contrib.auth import get_user_model
from knowledge.forms import ApprovedEntryChoice
from knowledge.models import Entry
from .models import Activity, Task

class PeopleChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.first_name or obj.username}（{obj.username}）"

class PeopleMultipleChoice(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.first_name or obj.username}（{obj.username}）"

class ActivityForm(forms.ModelForm):
    token = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    participants = PeopleMultipleChoice(label="获准参与成员", queryset=get_user_model().objects.filter(is_active=True).order_by("username"), required=False, widget=forms.CheckboxSelectMultiple, help_text="创建者自动加入；仅所选成员与负责人可查看本活动。")
    class Meta:
        model = Activity
        fields = ["title", "kind", "objective", "audience", "scheduled_at", "constraints", "plan", "participants"]
        labels = {"title": "活动或项目名称", "kind": "类型", "objective": "希望实现的目标", "audience": "参与对象说明", "scheduled_at": "计划时间", "constraints": "限制与待确认事项", "plan": "执行方案"}
        widgets = {"scheduled_at": forms.DateTimeInput(attrs={"type":"datetime-local"}, format="%Y-%m-%dT%H:%M"), "objective": forms.Textarea(attrs={"rows":3}), "constraints": forms.Textarea(attrs={"rows":3}), "plan": forms.Textarea(attrs={"rows":5})}
        help_texts = {"constraints": "未知场地、预算等请写待确认，不要编造。", "plan": "可暂时留空；本步手工填写，不自动生成策划。"}

class TaskForm(forms.ModelForm):
    token = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    assignee = PeopleChoice(label="指定承担人", queryset=get_user_model().objects.none(), required=False, empty_label="不指定，由活动成员主动认领")
    reference = ApprovedEntryChoice(label="参考经验", queryset=Entry.objects.filter(status="approved"), required=False, empty_label="暂不关联")
    class Meta:
        model = Task
        fields = ["title", "objective", "deliverable", "acceptance", "due_at", "assignee", "reference"]
        labels = {"title":"任务名称", "objective":"任务目标", "deliverable":"需要交付什么", "acceptance":"怎样算完成", "due_at":"截止时间"}
        widgets = {"due_at":forms.DateTimeInput(attrs={"type":"datetime-local"}, format="%Y-%m-%dT%H:%M"), "objective":forms.Textarea(attrs={"rows":2}), "deliverable":forms.Textarea(attrs={"rows":3}), "acceptance":forms.Textarea(attrs={"rows":3})}

    def __init__(self, *args, activity, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignee"].queryset = activity.participants.filter(is_active=True).order_by("username")
        from knowledge.services import searchable_entries
        self.fields["reference"].queryset = searchable_entries()

class ActionForm(forms.Form):
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    action = forms.ChoiceField(choices=[(x,x) for x in ["claim","accept","decline","block","unblock","cancel"]], widget=forms.HiddenInput)
    reason = forms.CharField(label="说明", max_length=1000, required=False, widget=forms.Textarea(attrs={"rows":3}))
