from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("mailing", "0003_emailtemplate_transactionalmessage_emailevent_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="emailevent",
            name="provider_event_id",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddIndex(
            model_name="emailevent",
            index=models.Index(fields=["provider_event_id"], name="email_events_provider_evt_idx"),
        ),
        migrations.AddConstraint(
            model_name="emailevent",
            constraint=models.UniqueConstraint(
                fields=("provider_event_id",),
                condition=~Q(provider_event_id=""),
                name="unique_nonempty_provider_event_id",
            ),
        ),
    ]
