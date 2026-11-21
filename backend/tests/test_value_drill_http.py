import os

os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/15')
os.environ.setdefault('SEED_DEMO', 'false')

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config.database import Base, get_db
from src.main import app
from src.models.user import User
from src.utils.password import hash_password


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)

    def override():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override
    with Session() as db:
        for role in ('admin', 'operator', 'viewer'):
            db.add(User(email=role + '@beiling.test', role=role,
                        password_hash=hash_password('EdgeStore!17')))
        db.commit()
    # The audit middleware keeps its own session factory; point it at the test
    # engine too so write requests do not hit an unrelated empty database.
    monkeypatch.setattr('src.middlewares.audit.Session', Session)
    # health() pings Redis; these tests only exercise the drill routes.
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()


def token(client, email):
    response = client.post('/auth/login', json={'email': email, 'password': 'EdgeStore!17'})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json()['token']}


def test_drill_http_flow_and_viewer_is_read_only(client):
    op = token(client, 'operator@beiling.test')
    created = client.post('/value-drills', headers=op, json={
        'name': 'drill-http', 'value_threshold': 4, 'value_capacity': 32,
    })
    assert created.status_code == 201, created.text
    ident = created.json()['id']

    put = client.post(f'/value-drills/{ident}/actions', headers=op,
                      json={'action': 'put', 'key': 'k', 'value': 'q' * 12})
    assert put.status_code == 200, put.text
    assert put.json()['drill']['occupancy']['used'] == 12

    viewer = token(client, 'viewer@beiling.test')
    assert client.get(f'/value-drills/{ident}', headers=viewer).status_code == 200
    denied = client.post(f'/value-drills/{ident}/actions', headers=viewer,
                         json={'action': 'verify'})
    assert denied.status_code == 403
    denied = client.post('/value-drills', headers=viewer, json={
        'name': 'nope', 'value_threshold': 4, 'value_capacity': 32,
    })
    assert denied.status_code == 403

    bad = client.post(f'/value-drills/{ident}/actions', headers=op,
                      json={'action': 'put', 'key': 'k', 'value': 'x' * 40})
    assert bad.status_code == 422

    verified = client.post(f'/value-drills/{ident}/actions', headers=op,
                           json={'action': 'verify'})
    assert verified.status_code == 200
    assert verified.json()['result']['ok'] is True

    assert client.get('/value-drills', headers=viewer).status_code == 200
    assert client.get('/value-drills/9999', headers=viewer).status_code == 404


def test_action_schema_rejects_missing_fields(client):
    op = token(client, 'operator@beiling.test')
    ident = client.post('/value-drills', headers=op, json={
        'name': 'drill-schema', 'value_threshold': 4, 'value_capacity': 32,
    }).json()['id']
    r = client.post(f'/value-drills/{ident}/actions', headers=op, json={'action': 'put'})
    assert r.status_code == 422
    r = client.post(f'/value-drills/{ident}/actions', headers=op,
                    json={'action': 'range_read', 'start': 'a'})
    assert r.status_code == 422


def test_range_read_hold_flag_is_honoured(client):
    op = token(client, 'operator@beiling.test')
    ident = client.post('/value-drills', headers=op, json={
        'name': 'drill-hold', 'value_threshold': 4, 'value_capacity': 32,
    }).json()['id']
    client.post(f'/value-drills/{ident}/actions', headers=op,
                json={'action': 'put', 'key': 'a', 'value': 'x' * 12})
    held = client.post(f'/value-drills/{ident}/actions', headers=op,
                       json={'action': 'range_read', 'start': 'a', 'end': 'z', 'hold': True})
    assert held.json()['result']['held'] is True
    assert held.json()['drill']['open_reads'] == 1
    release = client.post(f'/value-drills/{ident}/actions', headers=op,
                          json={'action': 'range_read', 'start': 'a', 'end': 'z', 'hold': False})
    body = release.json()['result']
    assert body['held'] is False
    assert body['finished']['verified'] is True
    # The first held read is still open; the transient one must not close it.
    assert release.json()['drill']['open_reads'] == 1
