from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0004_smartplaylist'),
    ]

    operations = [
        migrations.AddField(
            model_name='video',
            name='hidden',
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
