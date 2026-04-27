import flet as ft
import os
import functions as fn
from app_refs import (
    firefox_profile_path_ref,
    geocaching_username_ref,
    geocaching_password_ref,
    status_text_ref,
    loading_status_ref,
    progress_bar_ref,
    scan_button_ref,
    results_text_ref,
    csv_status_ref,
)


# Main function to run the Flet app
# --------------------------------------------------------------------------------
def main(page: ft.Page):

    def _env_bool(name: str, default: bool = False) -> bool:
        raw = (os.getenv(name, "") or "").strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    # Setup the Flet page
    page.title = "Manage Geocache Challenge Logs"
    page.vertical_alignment = ft.MainAxisAlignment.START
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.AUTO

    # ---- Splash screen -------------------------------------------------------

    page.add(
        ft.Text(
            "Manage Geocache Challenge Logs",
            size=24,
            weight=ft.FontWeight.BOLD,
            text_align=ft.TextAlign.CENTER,
            color=ft.Colors.WHITE,
        )
    )
    page.add(
        ft.Text(
            "Find all Write Note logs you have left on Challenge Caches.",
            size=14,
            text_align=ft.TextAlign.CENTER,
            color=ft.Colors.GREY_400,
        )
    )

    # Load persisted values, then optionally let .env values take precedence.
    persisted_username = page.client_storage.get("geocaching_username") or ""
    persisted_remember_password = bool(
        page.client_storage.get("remember_geocaching_password")
    )
    persisted_password = (
        page.client_storage.get("geocaching_password") or ""
        if persisted_remember_password
        else ""
    )
    persisted_profile_path = page.client_storage.get("firefox_profile_path") or ""

    env_username_set = os.environ.get("GEOCACHING_USERNAME") is not None
    env_password_set = os.environ.get("GEOCACHING_PASSWORD") is not None
    env_profile_path_set = os.environ.get("FIREFOX_PROFILE_PATH") is not None

    env_username = (os.getenv("GEOCACHING_USERNAME") or "").strip()
    env_password = os.getenv("GEOCACHING_PASSWORD") or ""
    env_profile_path = (os.getenv("FIREFOX_PROFILE_PATH") or "").strip()
    env_remember_set = (os.getenv("REMEMBER_GEOCACHING_PASSWORD", "") or "").strip() != ""
    env_remember_password = _env_bool("REMEMBER_GEOCACHING_PASSWORD", default=False)

    # Default behavior: if .env provides credentials, use them to prefill fields.
    prefer_env_defaults = _env_bool("GC_PREFER_ENV_CREDENTIALS", default=True)

    if prefer_env_defaults:
        stored_username = env_username if env_username_set else persisted_username
        stored_password = env_password if env_password_set else persisted_password
        stored_profile_path = (
            env_profile_path if env_profile_path_set else persisted_profile_path
        )
    else:
        stored_username = persisted_username or env_username
        stored_password = persisted_password or env_password
        stored_profile_path = persisted_profile_path or env_profile_path

    if env_remember_set:
        stored_remember_password = env_remember_password
    else:
        stored_remember_password = persisted_remember_password

    if prefer_env_defaults:
        username_source = (
            "env" if env_username_set else ("saved storage" if persisted_username else "none")
        )
        password_source = (
            "env" if env_password_set else ("saved storage" if persisted_password else "none")
        )
    else:
        username_source = (
            "saved storage" if persisted_username else ("env" if env_username_set else "none")
        )
        password_source = (
            "saved storage" if persisted_password else ("env" if env_password_set else "none")
        )

    # ---- Helper: enable/disable Start button --------------------------------
    def _update_start_button_state():
        username_ok = bool(
            (geocaching_username_ref.current.value or "").strip()
        )
        password_ok = bool(
            (geocaching_password_ref.current.value or "").strip()
        )
        start_button.disabled = not (username_ok and password_ok)
        start_button.update()

    def _on_password_change(e):
        if bool(page.client_storage.get("remember_geocaching_password")):
            page.client_storage.set(
                "geocaching_password",
                geocaching_password_ref.current.value or "",
            )
        _update_start_button_state()

    def _on_remember_password_change(e):
        remember = bool(e.control.value)
        page.client_storage.set("remember_geocaching_password", remember)
        if remember:
            page.client_storage.set(
                "geocaching_password",
                geocaching_password_ref.current.value or "",
            )
        else:
            page.client_storage.set("geocaching_password", "")

    # ---- Credential fields --------------------------------------------------
    username_field = ft.TextField(
        label="Geocaching username",
        value=stored_username,
        ref=geocaching_username_ref,
        on_change=lambda e: (
            page.client_storage.set("geocaching_username", e.control.value),
            _update_start_button_state(),
        ),
        width=360,
    )
    page.add(username_field)

    password_field = ft.TextField(
        label="Geocaching password",
        value=stored_password,
        ref=geocaching_password_ref,
        password=True,
        can_reveal_password=True,
        on_change=_on_password_change,
        width=360,
    )
    page.add(password_field)

    remember_checkbox = ft.Checkbox(
        label="Remember password",
        value=stored_remember_password,
        on_change=_on_remember_password_change,
    )
    page.add(remember_checkbox)

    fully_automated_checkbox = ft.Checkbox(
        label="Fully Automated - Change Write Note to Found",
        value=bool(page.client_storage.get("fully_automated_change_to_found") or False),
        on_change=lambda e: page.client_storage.set(
            "fully_automated_change_to_found", bool(e.control.value)
        ),
    )
    page.add(fully_automated_checkbox)

    # Optional Firefox profile path
    profile_field = ft.TextField(
        label="Firefox profile folder (optional – paste full path or leave blank)",
        value=stored_profile_path,
        ref=firefox_profile_path_ref,
        read_only=False,
        on_change=lambda e: page.client_storage.set(
            "firefox_profile_path", e.control.value
        ),
        width=500,
    )
    page.add(profile_field)

    # ---- Start button -------------------------------------------------------
    start_button = ft.CupertinoFilledButton(
        "Start",
        disabled=not (stored_username and stored_password),
    )

    def on_start_click(e):
        page.clean()

        fully_automated_mode = bool(fully_automated_checkbox.value)

        fn._log_message(
            "STARTUP | Credential source: "
            f"username={username_source}, password={password_source}, "
            f"prefer_env={prefer_env_defaults}"
        )

        # Loading status
        loading_status = ft.Text(
            "Launching Firefox and logging in… please wait.",
            ref=loading_status_ref,
            size=14,
            text_align=ft.TextAlign.CENTER,
            color=ft.Colors.LIGHT_BLUE,
        )
        page.add(loading_status)

        progress_bar = ft.ProgressBar(
            ref=progress_bar_ref,
            width=400,
            value=0.0,
            color=ft.Colors.BLUE,
        )
        page.add(progress_bar)

        username = (geocaching_username_ref.current.value or "").strip()
        password = geocaching_password_ref.current.value or ""

        try:
            driver = fn.initialize_driver(page, username=username, password=password)
        except Exception as exc:
            err = str(exc).strip() or "Startup failed."
            loading_status_ref.current.value = err
            loading_status_ref.current.color = ft.Colors.RED
            progress_bar_ref.current.visible = False
            loading_status_ref.current.update()
            progress_bar_ref.current.update()
            return

        # Update loading status
        loading_status_ref.current.value = (
            f"Logged in as {driver._gc_active_user}. Ready to scan. Log: {fn.get_log_file_path()}"
        )
        loading_status_ref.current.color = ft.Colors.GREEN
        progress_bar_ref.current.visible = False
        loading_status_ref.current.update()
        progress_bar_ref.current.update()

        # ---- Main screen (post-login) ----------------------------------------

        page.add(
            ft.Text(
                "Click 'Scan My Logs' to search all your Write Note logs for "
                "Challenge Caches.",
                size=14,
                text_align=ft.TextAlign.CENTER,
                color=ft.Colors.GREY_300,
            )
        )

        scan_progress_bar = ft.ProgressBar(
            width=400,
            value=0.0,
            color=ft.Colors.CYAN,
            visible=False,
        )
        page.add(scan_progress_bar)

        scan_progress_label = ft.Text(
            "",
            size=12,
            color=ft.Colors.GREY_400,
            text_align=ft.TextAlign.CENTER,
        )
        page.add(scan_progress_label)

        results_text = ft.Text(
            "",
            ref=results_text_ref,
            size=13,
            color=ft.Colors.YELLOW,
            text_align=ft.TextAlign.CENTER,
        )
        page.add(results_text)

        csv_status = ft.Text(
            "",
            ref=csv_status_ref,
            size=13,
            color=ft.Colors.LIGHT_BLUE,
            text_align=ft.TextAlign.CENTER,
        )
        page.add(csv_status)

        status_text = ft.Text(
            "",
            ref=status_text_ref,
            size=12,
            color=ft.Colors.GREY_400,
            text_align=ft.TextAlign.CENTER,
        )
        page.add(status_text)

        scan_launch_state = {"started": False}

        def _build_checker_summary(scan_results):
            total_checked = len(scan_results or [])
            statuses = [str((row or {}).get("checker_status") or "").strip() for row in (scan_results or [])]

            found_it_already = sum(1 for status in statuses if status == "Write Note + Found It")
            no_checker = sum(1 for status in statuses if status == "No automated checker available")
            failed_validation = sum(
                1 for status in statuses if status == "Checker indicates challenge not fulfilled"
            )
            passed_validation = sum(
                1
                for status in statuses
                if status in {"SUCCESS!", "Checker succeeded (no example log)"}
            )

            return (
                "Summary:\n"
                f"Listings checked: {total_checked}\n"
                f"Already Found It logs: {found_it_already}\n"
                f"No automated checker: {no_checker}\n"
                f"Checker failed validation: {failed_validation}\n"
                f"Checker PASSED! validation: {passed_validation}"
            )

        # ---- Scan button ----------------------------------------------------
        def on_scan_click(e):
            import threading

            if scan_launch_state["started"]:
                return
            scan_launch_state["started"] = True

            scan_button_ref.current.disabled = True
            scan_button_ref.current.update()
            scan_progress_bar.visible = True
            scan_progress_bar.value = 0.0
            scan_progress_bar.update()

            def update_scan_status(msg, color=None):
                if color is None:
                    color = ft.Colors.GREY_300
                status_text_ref.current.value = msg
                status_text_ref.current.color = color
                status_text_ref.current.update()

            def update_scan_progress(value, label=""):
                scan_progress_bar.value = value
                scan_progress_bar.update()
                scan_progress_label.value = label
                scan_progress_label.update()

            def run_scan():
                update_scan_status(
                    f"Scan started. Writing detailed logs to {fn.get_log_file_path()}",
                    ft.Colors.YELLOW,
                )
                try:
                    scan_results = fn.scan_challenge_write_notes(
                        driver,
                        status_callback=update_scan_status,
                        progress_callback=update_scan_progress,
                    )
                except Exception as exc:
                    fn._log_exception("SCAN_THREAD", exc)
                    status_text_ref.current.value = f"Scan failed: {exc}"
                    status_text_ref.current.color = ft.Colors.RED
                    status_text_ref.current.update()
                    scan_button_ref.current.disabled = False
                    scan_button_ref.current.update()
                    scan_progress_bar.visible = False
                    scan_progress_bar.update()
                    scan_progress_label.value = ""
                    scan_progress_label.update()
                    return

                count = len(scan_results)
                results_text_ref.current.value = (
                    f"Found {count} Write Note log{'s' if count != 1 else ''} "
                    f"on mystery Challenge Cache{'s' if count != 1 else ''}."
                )
                results_text_ref.current.color = (
                    ft.Colors.GREEN if count > 0 else ft.Colors.GREY_400
                )
                results_text_ref.current.update()

                if scan_results:
                    success, msg, csv_path = fn.export_to_csv(
                        scan_results,
                        status_callback=update_scan_status,
                    )
                    summary = _build_checker_summary(scan_results)
                    csv_status_ref.current.value = f"{msg}\n\n{summary}"
                    csv_status_ref.current.color = (
                        ft.Colors.GREEN if success else ft.Colors.RED
                    )
                    csv_status_ref.current.update()
                else:
                    csv_status_ref.current.value = _build_checker_summary(scan_results)
                    csv_status_ref.current.color = ft.Colors.GREY_400
                    csv_status_ref.current.update()

                if fully_automated_mode:
                    prepared, prep_msg = fn.prepare_write_note_edit_log_page(
                        driver,
                        scan_results,
                        status_callback=update_scan_status,
                    )
                    update_scan_status(
                        (
                            "Fully Automated mode enabled. "
                            f"{prep_msg} "
                            "Execution paused with browser intentionally left open for review/next-step logic."
                        ),
                        ft.Colors.GREEN if prepared else ft.Colors.YELLOW,
                    )
                    # fn.change_write_note_to_found(driver, scan_results)
                    return

                scan_button_ref.current.disabled = False
                scan_button_ref.current.update()
                scan_progress_bar.visible = False
                scan_progress_bar.update()
                scan_progress_label.value = ""
                scan_progress_label.update()
                status_text_ref.current.value = (
                    "Run complete. Please shut down the app using the red button at the top of the window."
                )
                status_text_ref.current.color = ft.Colors.YELLOW
                status_text_ref.current.update()

            thread = threading.Thread(target=run_scan, daemon=True)
            thread.start()

        scan_button = ft.CupertinoFilledButton(
            "Scan My Logs",
            ref=scan_button_ref,
            on_click=on_scan_click,
        )
        page.add(scan_button)

        status_text_ref.current.value = "Auto-starting scan in 5 seconds..."
        status_text_ref.current.color = ft.Colors.GREY_400
        status_text_ref.current.update()

        def auto_start_scan():
            import time

            time.sleep(5)
            if not scan_launch_state["started"]:
                on_scan_click(None)

        import threading
        threading.Thread(target=auto_start_scan, daemon=True).start()

    start_button.on_click = on_start_click
    page.add(start_button)


ft.app(target=main)
