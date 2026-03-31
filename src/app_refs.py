# app_refs.py
import flet as ft

# Define Flet Ref declarations for all UI controls
# -----------------------------------------------------------------------------
geocaching_username_ref = ft.Ref[ft.TextField]()
geocaching_password_ref = ft.Ref[ft.TextField]()
firefox_profile_path_ref = ft.Ref[ft.TextField]()
status_text_ref = ft.Ref[ft.Text]()
loading_status_ref = ft.Ref[ft.Text]()
progress_bar_ref = ft.Ref[ft.ProgressBar]()
scan_button_ref = ft.Ref[ft.CupertinoFilledButton]()
results_text_ref = ft.Ref[ft.Text]()
csv_status_ref = ft.Ref[ft.Text]()
