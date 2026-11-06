"""Image checks run with Docker networking disabled; never call a model."""

import asyncio
import json
import os
import urllib.error
import urllib.request


def request(path, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    try:
        with urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:8080" + path, headers=headers),
            timeout=5,
        ) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None


assert request("/health")[0] == 200
assert request("/api/console/requests")[0] == 401
assert request("/api/console/requests", os.environ["AISTUDIO_WEB_PASSWORD"])[0] == 200
assert request("/v1/auth/check", os.environ["AISTUDIO_WEB_PASSWORD"])[0] == 401
assert request("/v1/auth/check", os.environ["AISTUDIO_BOOTSTRAP_API_KEY"])[0] == 200
with urllib.request.urlopen("http://127.0.0.1:8081/health", timeout=5) as response:
    assert response.status == 200


async def check_browser():
    from playwright.async_api import async_playwright

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(
            executable_path="/opt/cloakbrowser/chrome",
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page()
        await page.goto("data:text/html,<title>offline engine check</title>")
        assert await page.title() == "offline engine check"
        await browser.close()


asyncio.run(check_browser())
print(
    json.dumps(
        {
            "ok": True,
            "networkDisabled": True,
            "modelCalls": 0,
            "localSignerHealthy": True,
        }
    )
)
