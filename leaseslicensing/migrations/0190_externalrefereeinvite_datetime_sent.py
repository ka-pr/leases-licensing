# Generated by Django 3.2.18 on 2023-06-16 03:12

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('leaseslicensing', '0189_auto_20230615_1205'),
    ]

    operations = [
        migrations.AddField(
            model_name='externalrefereeinvite',
            name='datetime_sent',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
