# Generated by Django 3.1.12 on 2021-08-18 08:06

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('perf', '0040_performancealert_noise_profile'),
    ]

    operations = [
        migrations.CreateModel(
            name='BackfillNotificationRecord',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name='ID'
                    ),
                ),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('last_updated', models.DateTimeField(auto_now=True)),
                (
                    'record',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='backfill_notification_record',
                        to='perf.backfillrecord',
                    ),
                ),
            ],
            options={
                'db_table': 'backfill_notification_record',
            },
        ),
    ]
