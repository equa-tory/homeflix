from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0006_playlist_thumbnail'),
    ]

    operations = [
        migrations.AddField(
            model_name='smartplaylist',
            name='thumbnail_path',
            field=models.CharField(blank=True, default='', max_length=1024),
        ),
    ]
