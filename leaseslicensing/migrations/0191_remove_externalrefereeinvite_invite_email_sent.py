# Generated by Django 3.2.18 on 2023-06-16 03:47

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('leaseslicensing', '0190_externalrefereeinvite_datetime_sent'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='externalrefereeinvite',
            name='invite_email_sent',
        ),
    ]
