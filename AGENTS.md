### Use UV for Python Package Management

When installing Python packages, use `uv` instead of `pip`.

Wrong:

```bash
pip install djangorestframework
```

Right:

```bash
uv add djangorestframework
```

Run Django commands with `uv run`:

```bash
uv run python manage.py migrate
uv run python manage.py test
```

### Deployment Environments

Datamailer currently has only a sandbox deployment. There is no Datamailer
production environment, so do not describe current work as production deploys
or production operations. Production-related docs and issues are future plans
unless a human explicitly confirms that production exists.
