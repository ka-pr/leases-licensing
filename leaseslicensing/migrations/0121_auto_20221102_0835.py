# Generated by Django 3.2.16 on 2022-11-02 00:35

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('leaseslicensing', '0120_auto_20221028_1409'),
    ]

    operations = [
        migrations.AddField(
            model_name='proposalassessment',
            name='referral',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='assessment', to='leaseslicensing.referral'),
        ),
        migrations.AlterField(
            model_name='proposalassessment',
            name='proposal',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='assessment', to='leaseslicensing.proposal'),
        ),
    ]