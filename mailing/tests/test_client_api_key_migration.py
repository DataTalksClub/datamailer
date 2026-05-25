import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)


def test_migration_preserves_existing_client_api_key_hash():
    executor = MigrationExecutor(connection)
    executor.migrate([("mailing", "0008_transactional_template_catalog")])
    old_apps = executor.loader.project_state([("mailing", "0008_transactional_template_catalog")]).apps
    Organization = old_apps.get_model("mailing", "Organization")
    Client = old_apps.get_model("mailing", "Client")

    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    client = Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
        api_key_hash="hashed-secret",
    )

    executor = MigrationExecutor(connection)
    executor.migrate([("mailing", "0009_multiple_client_api_keys")])
    new_apps = executor.loader.project_state([("mailing", "0009_multiple_client_api_keys")]).apps
    ClientApiKey = new_apps.get_model("mailing", "ClientApiKey")

    api_key = ClientApiKey.objects.get(client_id=client.id)
    assert api_key.name == "Migrated key"
    assert api_key.key_hash == "hashed-secret"
    assert api_key.public_id == f"legacy{client.id}"
    assert api_key.revoked_at is None
