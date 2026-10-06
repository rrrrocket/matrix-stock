from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('stock', '0002_orderalert')]

    operations = [
        migrations.AddField(
            model_name='externalorder',
            name='snapshot_version',
            field=models.PositiveBigIntegerField(default=0, verbose_name='ERP快照版本'),
        ),
    ]
