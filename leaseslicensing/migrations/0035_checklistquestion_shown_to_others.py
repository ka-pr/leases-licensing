# Generated by Django 3.2.4 on 2022-02-10 02:00

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("leaseslicensing", "0034_merge_20220208_0854"),
    ]

    operations = [
        migrations.AddField(
            model_name="checklistquestion",
            name="shown_to_others",
            field=models.BooleanField(default=False, verbose_name="Comment"),
        ),
    ]
