# Generated by Django 3.2.4 on 2022-02-04 10:28

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('leaseslicensing', '0028_auto_20220203_1633'),
    ]

    operations = [
        migrations.AlterField(
            model_name='proposal',
            name='submitter',
            field=models.IntegerField(null=True),
        ),
    ]
