from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('stock', '0004_erplinkgrant')]

    operations = [
        migrations.AddField(model_name='stockmovement', name='tracking_number', field=models.CharField(blank=True, max_length=200, verbose_name='国内快递单号')),
        migrations.AddField(model_name='stockmovement', name='erp_callback_sent_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='stockmovement', name='erp_callback_attempts', field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name='stockmovement', name='erp_callback_next_attempt_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='stockmovement', name='erp_callback_error', field=models.CharField(blank=True, max_length=500)),
    ]
