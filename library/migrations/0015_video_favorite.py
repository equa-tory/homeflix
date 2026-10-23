from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0014_likes'),
    ]

    operations = [
        migrations.AddField(
            model_name='video',
            name='favorite',
            field=models.BooleanField(default=False),
        ),
    ]
