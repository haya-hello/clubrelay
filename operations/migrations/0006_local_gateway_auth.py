# 本机接口凭据显式开关。 / Explicit credential opt-in for a local gateway.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('operations', '0005_clubrelay'),
    ]

    operations = [
        migrations.AddField(
            model_name='modelconfiguration',
            name='local_auth',
            field=models.BooleanField(default=False),
        ),
    ]
