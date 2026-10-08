from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0012_video_not_short'),
    ]

    operations = [
        migrations.CreateModel(
            name='LibraryFolder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('path', models.CharField(max_length=1024, unique=True)),
                ('remote', models.CharField(blank=True, default='', max_length=1024)),
                ('is_main', models.BooleanField(default=False)),
                ('position', models.PositiveIntegerField(default=0)),
            ],
            options={
                'ordering': ['position', 'id'],
            },
        ),
    ]
