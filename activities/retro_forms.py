from django import forms

class RetroForm(forms.Form):
    revision = forms.IntegerField(min_value=1,widget=forms.HiddenInput)
    facts = forms.CharField(label="实际结果（人工核实）",max_length=5000,required=False,widget=forms.Textarea(attrs={"rows":4}))
    differences = forms.CharField(label="计划与实际的差异",max_length=5000,required=False,widget=forms.Textarea(attrs={"rows":3}))
    hypotheses = forms.CharField(label="原因假设（不是已确认事实）",max_length=5000,required=False,widget=forms.Textarea(attrs={"rows":3}))
    improvements = forms.CharField(label="下次的具体改进",max_length=5000,required=False,widget=forms.Textarea(attrs={"rows":3}))
    applicability = forms.CharField(label="适用边界与待核验事项",max_length=3000,required=False,widget=forms.Textarea(attrs={"rows":3}))

class RetroActionForm(forms.Form):
    revision = forms.IntegerField(min_value=1)
    action = forms.ChoiceField(choices=[("confirm","确认"),("withdraw","撤回")])
    acknowledged = forms.BooleanField(required=False)
    reason = forms.CharField(max_length=1000,required=False)

class KnowledgeExportForm(forms.Form):
    revision = forms.IntegerField(min_value=1,widget=forms.HiddenInput)
    title = forms.CharField(label="可复用经验标题",max_length=120)
    body = forms.CharField(label="准备进入知识库的内容",max_length=15000,widget=forms.Textarea(attrs={"rows":10}))
    applicability = forms.CharField(label="这条经验的适用边界",max_length=500,widget=forms.Textarea(attrs={"rows":2}))
    share_ack = forms.BooleanField(label="我已检查，这段经验可供全社团成员查看")

class StageForm(forms.Form):
    version = forms.IntegerField(min_value=1)
    stage = forms.ChoiceField(choices=[("active","执行中"),("wrapup","收尾中"),("archived","已归档")])
    reason = forms.CharField(max_length=1000)

