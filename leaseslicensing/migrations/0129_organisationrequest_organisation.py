# Generated by Django 3.2.18 on 2023-03-20 03:55

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('leaseslicensing', '0128_organisation_lodgement_number'),
    ]

    # I want organisation field on organisation request to be non nullable so we must have a default value
    # (even if there are not yet any records in the database)
    operations = [
        migrations.AddField(
            model_name='organisationrequest',
            name='organisation',
            field=models.ForeignKey(default=1, on_delete=django.db.models.deletion.PROTECT, related_name='organisation_requests', to='leaseslicensing.organisation'),
        ),
    ]