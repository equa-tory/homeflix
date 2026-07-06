from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0005_video_hidden'),
    ]

    operations = [
        migrations.AddField(
            model_name='playlist',
            name='thumbnail_path',
            field=models.CharField(blank=True, default='', max_length=1024),
        ),
    ]
