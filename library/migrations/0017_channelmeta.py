from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0016_channelsub'),
    ]

    operations = [
        migrations.CreateModel(
            name='ChannelMeta',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=256, unique=True)),
                ('channel_id', models.CharField(blank=True, default='', max_length=64)),
                ('avatar_updated', models.DateTimeField(blank=True, null=True)),
            ],
        ),
    ]
