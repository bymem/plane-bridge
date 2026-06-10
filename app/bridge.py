import os
import time
import json
import requests
from pathlib import Path

STATE_FILE = Path("/data/synced.json")


def env(key, default=None):
    value = os.getenv(key, default)
    if value is None:
        raise RuntimeError(f"Missing env var: {key}")
    return value


def headers(api_key):
    return {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_state_map():
    raw = env("STATE_MAP", "{}")
    state_map = json.loads(raw)
    reverse_map = {ce_id: cloud_id for cloud_id, ce_id in state_map.items()}
    return state_map, reverse_map


def api_url(base, workspace, project, item_id=None):
    url = f"{base}/api/v1/workspaces/{workspace}/projects/{project}/work-items/"
    if item_id:
        url += f"{item_id}/"
    return url


def get_description(item):
    return (
        item.get("description_html")
        or item.get("description")
        or ""
    )


def list_items(base, key, workspace, project):
    url = api_url(base, workspace, project)
    items = []
    cursor = None

    while True:
        params = {"per_page": 100}
        if cursor:
            params["cursor"] = cursor

        r = requests.get(url, headers=headers(key), params=params, timeout=30)
        r.raise_for_status()
        data = r.json()

        batch = data.get("results", data if isinstance(data, list) else [])
        items.extend(batch)

        cursor = data.get("next_cursor")
        if not cursor or not data.get("next_page_results"):
            break

    return items


def get_item(base, key, workspace, project, item_id):
    r = requests.get(
        api_url(base, workspace, project, item_id),
        headers=headers(key),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def update_item(base, key, workspace, project, item_id, body):
    r = requests.patch(
        api_url(base, workspace, project, item_id),
        headers=headers(key),
        json=body,
        timeout=30,
    )

    if not r.ok:
        print("Update failed:", r.status_code, r.text, flush=True)

    r.raise_for_status()
    return r.json()


def create_item_in_ce(cloud_item):
    state_map, _ = load_state_map()

    body = {
        "name": cloud_item.get("name") or "Untitled note",
        "description_html": get_description(cloud_item),
        "priority": cloud_item.get("priority") or "none",
    }

    ce_sync_label_id = os.getenv("CE_SYNC_LABEL_ID")
    if ce_sync_label_id:
        body["labels"] = [ce_sync_label_id]

    cloud_state = cloud_item.get("state")
    ce_state = state_map.get(cloud_state)

    if ce_state:
        body["state"] = ce_state

    r = requests.post(
        api_url(
            env("CE_BASE_URL"),
            env("CE_WORKSPACE"),
            env("CE_PROJECT_ID"),
        ),
        headers=headers(env("CE_API_KEY")),
        json=body,
        timeout=30,
    )

    if not r.ok:
        print("Create CE item failed:", r.status_code, r.text, flush=True)

    r.raise_for_status()
    return r.json()


def sync_state_pair(cloud_item, ce_item, record):
    state_map, reverse_state_map = load_state_map()

    cloud_id = cloud_item["id"]
    ce_id = ce_item["id"]

    cloud_state = cloud_item.get("state")
    ce_state = ce_item.get("state")

    last_cloud_state = record.get("last_cloud_state")
    last_ce_state = record.get("last_ce_state")

    cloud_changed = cloud_state != last_cloud_state
    ce_changed = ce_state != last_ce_state

    if not cloud_changed and not ce_changed:
        return record

    print(
        f"State check {cloud_item.get('name')}: "
        f"cloud_changed={cloud_changed}, ce_changed={ce_changed}",
        flush=True,
    )

    if cloud_changed and not ce_changed:
        new_ce_state = state_map.get(cloud_state)

        if new_ce_state and new_ce_state != ce_state:
            print(f"Cloud state changed. Updating CE {ce_id}", flush=True)
            ce_item = update_item(
                env("CE_BASE_URL"),
                env("CE_API_KEY"),
                env("CE_WORKSPACE"),
                env("CE_PROJECT_ID"),
                ce_id,
                {"state": new_ce_state},
            )
            ce_state = ce_item.get("state")

    elif ce_changed and not cloud_changed:
        new_cloud_state = reverse_state_map.get(ce_state)

        if new_cloud_state and new_cloud_state != cloud_state:
            print(f"CE state changed. Updating Cloud {cloud_id}", flush=True)
            cloud_item = update_item(
                env("CLOUD_BASE_URL"),
                env("CLOUD_API_KEY"),
                env("CLOUD_WORKSPACE"),
                env("CLOUD_PROJECT_ID"),
                cloud_id,
                {"state": new_cloud_state},
            )
            cloud_state = cloud_item.get("state")

    elif cloud_changed and ce_changed:
        # Conflict rule: CE wins.
        new_cloud_state = reverse_state_map.get(ce_state)

        if new_cloud_state and new_cloud_state != cloud_state:
            print(f"State conflict. CE wins. Updating Cloud {cloud_id}", flush=True)
            cloud_item = update_item(
                env("CLOUD_BASE_URL"),
                env("CLOUD_API_KEY"),
                env("CLOUD_WORKSPACE"),
                env("CLOUD_PROJECT_ID"),
                cloud_id,
                {"state": new_cloud_state},
            )
            cloud_state = cloud_item.get("state")

    record["last_cloud_state"] = cloud_state
    record["last_ce_state"] = ce_state

    return record


def sync_text_pair(cloud_item, ce_item, record):
    cloud_id = cloud_item["id"]
    ce_id = ce_item["id"]

    cloud_name = cloud_item.get("name") or ""
    ce_name = ce_item.get("name") or ""

    cloud_desc = get_description(cloud_item)
    ce_desc = get_description(ce_item)

    last_cloud_name = record.get("last_cloud_name", cloud_name)
    last_ce_name = record.get("last_ce_name", ce_name)
    last_cloud_desc = record.get("last_cloud_description_html", cloud_desc)
    last_ce_desc = record.get("last_ce_description_html", ce_desc)

    cloud_changed = cloud_name != last_cloud_name or cloud_desc != last_cloud_desc
    ce_changed = ce_name != last_ce_name or ce_desc != last_ce_desc

    if not cloud_changed and not ce_changed:
        return record

    print(
        f"Text check {cloud_name}: "
        f"cloud_changed={cloud_changed}, ce_changed={ce_changed}",
        flush=True,
    )

    # print("Cloud desc:", repr(cloud_desc), flush=True)
    # print("CE desc:", repr(ce_desc), flush=True)

    if cloud_changed and not ce_changed:
        print(f"Cloud text changed. Updating CE {ce_id}", flush=True)

        ce_item = update_item(
            env("CE_BASE_URL"),
            env("CE_API_KEY"),
            env("CE_WORKSPACE"),
            env("CE_PROJECT_ID"),
            ce_id,
            {
                "name": cloud_name,
                "description_html": cloud_desc,
            },
        )

        ce_name = ce_item.get("name") or ""
        ce_desc = get_description(ce_item)

    elif ce_changed and not cloud_changed:
        print(f"CE text changed. Updating Cloud {cloud_id}", flush=True)

        cloud_item = update_item(
            env("CLOUD_BASE_URL"),
            env("CLOUD_API_KEY"),
            env("CLOUD_WORKSPACE"),
            env("CLOUD_PROJECT_ID"),
            cloud_id,
            {
                "name": ce_name,
                "description_html": ce_desc,
            },
        )

        # print("Cloud update response:", json.dumps(cloud_item, indent=2), flush=True)

        cloud_name = cloud_item.get("name") or ""
        cloud_desc = get_description(cloud_item)

    elif cloud_changed and ce_changed:
        # Conflict rule: CE wins.
        print(f"Text conflict. CE wins. Updating Cloud {cloud_id}", flush=True)

        cloud_item = update_item(
            env("CLOUD_BASE_URL"),
            env("CLOUD_API_KEY"),
            env("CLOUD_WORKSPACE"),
            env("CLOUD_PROJECT_ID"),
            cloud_id,
            {
                "name": ce_name,
                "description_html": ce_desc,
            },
        )

        print("Cloud conflict update response:", json.dumps(cloud_item, indent=2), flush=True)

        cloud_name = cloud_item.get("name") or ""
        cloud_desc = get_description(cloud_item)

    record["last_cloud_name"] = cloud_name
    record["last_ce_name"] = ce_name
    record["last_cloud_description_html"] = cloud_desc
    record["last_ce_description_html"] = ce_desc

    return record


def run_once():
    state_map, _ = load_state_map()
    if not state_map:
        print("WARNING: STATE_MAP is empty. State sync disabled.", flush=True)

    state = load_state()

    print("Checking Cloud project...", flush=True)

    cloud_items = list_items(
        env("CLOUD_BASE_URL"),
        env("CLOUD_API_KEY"),
        env("CLOUD_WORKSPACE"),
        env("CLOUD_PROJECT_ID"),
    )

    print(f"Found {len(cloud_items)} Cloud items", flush=True)

    for cloud_item in cloud_items:
        cloud_id = cloud_item["id"]

        if cloud_id not in state:
            print(f"Creating CE item from Cloud item: {cloud_item.get('name')}", flush=True)
            ce_item = create_item_in_ce(cloud_item)

            state[cloud_id] = {
                "ce_id": ce_item["id"],
                "last_cloud_state": cloud_item.get("state"),
                "last_ce_state": ce_item.get("state"),
                "last_cloud_name": cloud_item.get("name") or "",
                "last_ce_name": ce_item.get("name") or "",
                "last_cloud_description_html": get_description(cloud_item),
                "last_ce_description_html": get_description(ce_item),
            }

            save_state(state)
            continue

        record = state[cloud_id]

        # Upgrade old synced.json format automatically.
        if isinstance(record, str):
            record = {"ce_id": record}
            state[cloud_id] = record

        ce_id = record["ce_id"]

        try:
            ce_item = get_item(
                env("CE_BASE_URL"),
                env("CE_API_KEY"),
                env("CE_WORKSPACE"),
                env("CE_PROJECT_ID"),
                ce_id,
            )
        except requests.HTTPError as e:
            print(f"Could not fetch CE item {ce_id}: {e}", flush=True)
            continue

        record = sync_state_pair(cloud_item, ce_item, record)

        # Re-fetch CE after possible state update, so text sync uses fresh data.
        ce_item = get_item(
            env("CE_BASE_URL"),
            env("CE_API_KEY"),
            env("CE_WORKSPACE"),
            env("CE_PROJECT_ID"),
            ce_id,
        )

        record = sync_text_pair(cloud_item, ce_item, record)

        state[cloud_id] = record
        save_state(state)


def main():
    print("Plane bridge started", flush=True)
    print("Cloud:", env("CLOUD_BASE_URL"), env("CLOUD_WORKSPACE"), env("CLOUD_PROJECT_ID"), flush=True)
    print("CE:", env("CE_BASE_URL"), env("CE_WORKSPACE"), env("CE_PROJECT_ID"), flush=True)

    while True:
        try:
            run_once()
        except Exception as e:
            print("Sync failed:", repr(e), flush=True)

        time.sleep(int(env("POLL_SECONDS", "180")))


if __name__ == "__main__":
    main()