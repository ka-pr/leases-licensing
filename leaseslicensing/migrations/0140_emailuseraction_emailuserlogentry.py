# Generated by Django 3.2.18 on 2023-03-30 03:18

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('leaseslicensing', '0139_auto_20230330_1015'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmailUserAction',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('who', models.IntegerField()),
                ('who_full_name', models.CharField(default='Anonymous User', max_length=200)),
                ('when', models.DateTimeField(auto_now_add=True)),
                ('what', models.TextField()),
                ('email_user', models.IntegerField()),
            ],
        ),
        migrations.CreateModel(
            name='EmailUserLogEntry',
            fields=[
                ('communicationslogentry_ptr', models.OneToOneField(auto_created=True, on_delete=django.db.models.deletion.CASCADE, parent_link=True, primary_key=True, serialize=False, to='leaseslicensing.communicationslogentry')),
                ('email_user', models.IntegerField()),
            ],
            bases=('leaseslicensing.communicationslogentry',),
        ),
    ]