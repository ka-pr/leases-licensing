# Generated by Django 3.2.4 on 2022-02-01 00:56

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("leaseslicensing", "0019_auto_20220128_1434"),
    ]

    operations = [
        migrations.DeleteModel(
            name="ProposalApproverGroup",
        ),
        migrations.DeleteModel(
            name="ProposalAssessorGroup",
        ),
    ]
