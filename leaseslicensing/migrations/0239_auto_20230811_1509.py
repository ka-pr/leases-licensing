# Generated by Django 3.2.18 on 2023-08-11 07:09

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('leaseslicensing', '0238_alter_customcpiyear_percentage'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='consumerpriceindex',
            options={'ordering': ['-year', '-quarter'], 'verbose_name': 'CPI Data (Perth - All Groups)', 'verbose_name_plural': 'CPI Data (Perth - All Groups)'},
        ),
        migrations.RemoveField(
            model_name='consumerpriceindex',
            name='time_period',
        ),
        migrations.AddField(
            model_name='consumerpriceindex',
            name='quarter',
            field=models.PositiveSmallIntegerField(null=True),
        ),
        migrations.AddField(
            model_name='consumerpriceindex',
            name='year',
            field=models.PositiveSmallIntegerField(null=True),
        ),
    ]
