import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0007_smartplaylist_thumbnail'),
    ]

    operations = [
        migrations.CreateModel(
            name='VideoSubtitle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(max_length=120)),
                ('lang', models.CharField(blank=True, default='', max_length=16)),
                ('vtt_path', models.CharField(max_length=1024)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('video', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='uploaded_subtitles', to='library.video')),
            ],
            options={
                'ordering': ['id'],
            },
        ),
    ]
