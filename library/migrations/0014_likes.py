import json

from django.db import migrations, models
from django.db.models import F

# A favorite used to outrank any star rating. It becomes this many likes on top
# of the stars, so a favorite lands level with a 5-star video and above the rest.
FAVORITE_AS_LIKES = 5


def fold_favorites_into_likes(apps, schema_editor):
    Video = apps.get_model("library", "Video")
    Video.objects.filter(favorite=True).update(rating=F("rating") + FAVORITE_AS_LIKES)

    # Smart playlists: "is favorite" is gone; the same videos are "likes >= 5".
    # ("rating" rules keep their meaning: stars were 1-5, likes start from that.)
    SmartPlaylist = apps.get_model("library", "SmartPlaylist")
    for sp in SmartPlaylist.objects.all():
        try:
            rules = json.loads(sp.rules)
        except ValueError:
            continue
        changed = False
        for r in rules if isinstance(rules, list) else []:
            if isinstance(r, dict) and r.get("field") == "favorite":
                r.update(field="rating", op="gte", value=str(FAVORITE_AS_LIKES))
                changed = True
        if changed:
            sp.rules = json.dumps(rules)
            sp.save(update_fields=["rules"])


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0013_libraryfolder'),
    ]

    operations = [
        migrations.RunPython(fold_favorites_into_likes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='video',
            name='rating',
            field=models.IntegerField(default=0),
        ),
        migrations.RemoveField(
            model_name='video',
            name='favorite',
        ),
    ]
