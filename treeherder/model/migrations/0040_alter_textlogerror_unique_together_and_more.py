# Generated by Django 5.1.2 on 2025-03-18 17:45

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("model", "0039_fix_bugscache_autoincrement"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="textlogerror",
            unique_together={("job", "line_number")},
        ),
        migrations.RemoveField(
            model_name="textlogerror",
            name="step",
        ),
        migrations.DeleteModel(
            name="TextLogStep",
        ),
    ]
