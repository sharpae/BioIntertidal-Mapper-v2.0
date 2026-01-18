"""
BioIntertidal Mapper (GUI)

Tkinter application that authenticates with Google Earth Engine (GEE), filters
Sentinel‑2 imagery by cloud/water thresholds, and exports NDVI + RGB composites
to Google Drive.
"""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import re
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import Optional

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

APP_TITLE = "BioIntertidal Mapper - Version 2.0"

SENTINEL2_COLLECTION_ID = "COPERNICUS/S2_SR_HARMONIZED"

REQUIRED_PIP_PACKAGES: tuple[str, ...] = ("earthengine-api", "certifi")


def _is_frozen_executable() -> bool:
    """True when running from a bundled executable (e.g., PyInstaller)."""

    # PyInstaller sets sys.frozen = True and typically provides sys._MEIPASS.
    return bool(getattr(sys, "frozen", False))

LOGIN_BUTTON_TEXT_DEFAULT = "Log in to Google Earth Engine"
LOGIN_BUTTON_TEXT_WORKING = "Logging in..."
LOGIN_BUTTON_TEXT_INSTALLING_DEPS = "Installing dependencies..."
LOGIN_BUTTON_TEXT_AUTHENTICATED = "Logged in ✓ (Re-authenticate)"

DEFAULT_ENTRY_VALUES: dict[str, str] = {
    "start_date": "2021-09-05",
    "end_date": "2021-09-25",
    "cloudy_percentage": "20",
    "max_water_percentage": "20",
    "tile": "T29SQV",
    "start_ndvi": "0.08",
    "end_ndvi": "1",
    "geometry": "projects/biointertidalmapper/assets/AOI_LosLances_beach_32629",
    "epsg": "32629",
    "folder_name": "LosLancesBeach",
}


@dataclass(frozen=True)
class MapperParams:
    start_date: str
    end_date: str
    cloudy_percentage: int
    max_water_percentage: int
    tile: str
    start_ndvi: float
    end_ndvi: float
    geometry_asset_id: str
    epsg: int
    drive_folder: str


class TextRedirector:
    """Redirects writes (stdout/stderr) to a Tkinter Text widget safely."""

    def __init__(self, text_widget: tk.Text) -> None:
        self._text_widget = text_widget

    def write(self, text: str) -> None:
        if not text:
            return
        self._text_widget.after(0, self._append, text)

    def flush(self) -> None:
        return

    def _append(self, text: str) -> None:
        self._text_widget.configure(state="normal")
        self._text_widget.insert(tk.END, text)
        self._text_widget.see(tk.END)
        self._text_widget.configure(state="disabled")


def _pip_install(packages: list[str]) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", *packages])


def _configure_ssl_with_certifi(certifi_module) -> None:
    try:
        context = ssl.create_default_context(cafile=certifi_module.where())

        def _create_https_context() -> ssl.SSLContext:
            return context

        ssl._create_default_https_context = _create_https_context
    except Exception as exc:
        print(f"Warning: Failed to set SSL context: {exc}")


def _parse_tile(value: str) -> str:
    return value.strip().replace(" ", "").upper()


def _parse_int(value: str, field_name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc


def _parse_float(value: str, field_name: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number.") from exc


def _validate_params(params: MapperParams) -> tuple[bool, str]:
    if not params.tile:
        return False, "Sentinel-2 tile must not be empty. Example: T29UPV"
    if not params.geometry_asset_id.strip():
        return False, "Geometry asset id must not be empty."
    if not params.drive_folder.strip():
        return False, "Google Drive folder name must not be empty."

    if ";" in params.tile:
        return False, "Only one Sentinel-2 tile is supported. Example: T29UPV"

    tile_pattern = re.compile(r"^T\d{2}[A-Z]{3}$")
    if not tile_pattern.fullmatch(params.tile):
        return False, "Sentinel-2 tile must match the pattern (e.g., T29UPV)."

    try:
        start_dt = date.fromisoformat(params.start_date)
        end_dt = date.fromisoformat(params.end_date)
    except ValueError:
        return False, "Date range must be in the format YYYY-MM-DD."

    if start_dt < date(2017, 3, 1):
        return False, "Start date must be after March 2017."
    if end_dt < start_dt:
        return False, "End date must be on or after start date."

    if not (0 <= params.cloudy_percentage <= 100):
        return False, "Cloud percentage must be between 0 and 100."
    if not (0 <= params.max_water_percentage <= 100):
        return False, "Max water percentage must be between 0 and 100."

    if not (-1 <= params.start_ndvi <= 1 and -1 <= params.end_ndvi <= 1):
        return False, "NDVI range must be between -1 and 1."
    if params.end_ndvi <= params.start_ndvi:
        return False, "NDVI end value must be greater than the start value."

    if params.epsg <= 0:
        return False, "EPSG must be a positive integer (e.g., 32629)."

    return True, ""


def _build_image_collection(ee_module, geometry_fc, params: MapperParams):
    aoi = geometry_fc.geometry()

    def calculate_ndwi(image):
        ndwi = image.normalizedDifference(["B3", "B8"]).rename("ndwi")
        water_pixels = ndwi.gt(0)
        water_fraction = water_pixels.reduceRegion(
            reducer=ee_module.Reducer.mean(),
            geometry=aoi,
            scale=10,
            maxPixels=1e9,
        ).get("ndwi")
        return image.set("water_percentage", ee_module.Number(water_fraction).multiply(100))

    return (
        ee_module.ImageCollection(SENTINEL2_COLLECTION_ID)
        .filterDate(params.start_date, params.end_date)
        .filterBounds(aoi)
        .filter(ee_module.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", params.cloudy_percentage))
        .filter(ee_module.Filter.stringContains("system:index", params.tile))
        .map(lambda image: image.clip(aoi))
        .map(calculate_ndwi)
        .filter(ee_module.Filter.lt("water_percentage", params.max_water_percentage))
    )


def _export_ndvi_and_rgb(
    ee_module,
    image,
    image_date: str,
    region_geometry,
    params: MapperParams,
    tile: str,
    is_first: bool = False,
) -> None:
    ndvi = image.normalizedDifference(["B8", "B4"]).rename("ndvi")
    ndvi_masked = ndvi.updateMask(ndvi.gt(params.start_ndvi).And(ndvi.lt(params.end_ndvi)))
    ndvi_name = f"ndvi_{image_date}_{tile}".replace("-", "")

    task_ndvi = ee_module.batch.Export.image.toDrive(
        image=ndvi_masked,
        description=ndvi_name,
        scale=10,
        fileNamePrefix=ndvi_name,
        folder=params.drive_folder,
        maxPixels=1e13,
        crs=f"EPSG:{params.epsg}",
        region=region_geometry,
    )
    task_ndvi.start()
    print(f"Image '{params.drive_folder}/{ndvi_name}' exported successfully")

    # Wait after the very first export task for Google Drive to create the folder
    if is_first:
        print("Waiting for Google Drive folder creation...")
        time.sleep(5)

    rgb = image.select(["B4", "B3", "B2"])
    rgb_name = f"rgb_{image_date}_{tile}".replace("-", "")
    task_rgb = ee_module.batch.Export.image.toDrive(
        image=rgb,
        description=rgb_name,
        scale=10,
        fileNamePrefix=rgb_name,
        folder=params.drive_folder,
        maxPixels=1e13,
        crs=f"EPSG:{params.epsg}",
        region=region_geometry,
    )
    task_rgb.start()
    print(f"Image '{params.drive_folder}/{rgb_name}' exported successfully")
    print()


class BioIntertidalMapperApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)

        self._ee = None
        self._dependency_install_in_progress = False
        self._configure_layout()
        self._create_widgets()
        self._redirect_output()
        self.root.after(0, self._check_dependencies_on_startup)

    def run(self) -> None:
        self.root.mainloop()

    # ----------------------------- UI helpers -----------------------------

    def _call_in_ui(self, func, *args, **kwargs) -> None:
        self.root.after(0, partial(func, *args, **kwargs))

    def _set_progress(self, value: float) -> None:
        self._call_in_ui(self._set_progress_ui, value)

    def _set_progress_ui(self, value: float) -> None:
        self.progress_bar["value"] = max(0.0, min(100.0, float(value)))

    def _set_run_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        if threading.current_thread() is threading.main_thread():
            self.run_button.configure(state=state)
        else:
            self._call_in_ui(self.run_button.configure, state=state)

    def _set_login_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        if threading.current_thread() is threading.main_thread():
            self.login_button.configure(state=state)
        else:
            self._call_in_ui(self.login_button.configure, state=state)

    def _show_error(self, title: str, message: str) -> None:
        self._call_in_ui(messagebox.showerror, title, message)

    def _show_info(self, title: str, message: str) -> None:
        self._call_in_ui(messagebox.showinfo, title, message)

    # ---------------------------- Dependencies ----------------------------

    def _ensure_dependencies(self, *, prompt: bool = True) -> bool:
        if self._ee is not None:
            return True

        if prompt and threading.current_thread() is not threading.main_thread():
            prompt = False

        try:
            ee_module = importlib.import_module("ee")
            certifi_module = importlib.import_module("certifi")
        except ImportError as exc:
            packages = self._detect_missing_pip_packages()
            packages_to_install = packages or list(REQUIRED_PIP_PACKAGES)

            if prompt:
                self._prompt_install_dependencies(packages_to_install, import_error=str(exc))
                return False

            self._show_error(
                "Missing Dependencies",
                "Missing required Python packages.\n\n"
                "Install dependencies with:\n"
                f"  {sys.executable} -m pip install {' '.join(packages_to_install)}\n\n"
                f"Details: {exc}",
            )
            return False
        except Exception as exc:
            self._show_error(
                "Dependency Error",
                "Failed to import required Python packages.\n\n"
                f"Details: {exc}",
            )
            return False

        _configure_ssl_with_certifi(certifi_module)
        self._ee = ee_module
        return True

    def _check_dependencies_on_startup(self) -> None:
        try:
            missing = self._detect_missing_pip_packages()
        except Exception as exc:
            self._show_error("Dependency Check Failed", str(exc))
            return
        if missing:
            self._prompt_install_dependencies(missing)

    def _detect_missing_pip_packages(self) -> list[str]:
        missing: list[str] = []
        if importlib.util.find_spec("ee") is None:
            missing.append("earthengine-api")
        if importlib.util.find_spec("certifi") is None:
            missing.append("certifi")
        return missing

    def _prompt_install_dependencies(self, packages: list[str], *, import_error: Optional[str] = None) -> None:
        if self._dependency_install_in_progress:
            messagebox.showinfo("Dependencies", "Dependency installation is already in progress.")
            return

        self.auth_status_var.set("Status: Missing dependencies")
        packages_text = "\n".join(f"- {pkg}" for pkg in packages)
        cmd = f"{sys.executable} -m pip install {' '.join(packages)}"
        details = f"\n\nImport error:\n{import_error}" if import_error else ""
        message = (
            "This application requires additional Python packages:\n\n"
            f"{packages_text}\n\n"
            "Install them now using pip? (Internet connection required)\n\n"
            f"Command:\n{cmd}"
            f"{details}"
        )

        if messagebox.askyesno("Install Dependencies", message):
            self._start_dependency_install(packages)
        else:
            self.auth_status_var.set("Status: Dependencies required")
            messagebox.showinfo(
                "Dependencies Required",
                "You can install dependencies later with:\n\n"
                f"{cmd}",
            )

    def _start_dependency_install(self, packages: list[str]) -> None:
        if self._dependency_install_in_progress:
            return

        self._dependency_install_in_progress = True
        self.login_button.configure(text=LOGIN_BUTTON_TEXT_INSTALLING_DEPS)
        self.auth_status_var.set("Status: Installing dependencies...")
        self._set_login_enabled(False)
        print()
        print(f"Installing dependencies: {', '.join(packages)}")
        print(f"Running: {sys.executable} -m pip install {' '.join(packages)}")
        print()

        threading.Thread(target=self._dependency_install_worker, args=(packages,), daemon=True).start()

    def _dependency_install_worker(self, packages: list[str]) -> None:
        cmd = [sys.executable, "-m", "pip", "install", *packages]
        try:
            _pip_install(packages)
        except subprocess.CalledProcessError as exc:
            self._show_error(
                "Dependency Installation Failed",
                "pip failed to install dependencies.\n\n"
                f"Command:\n{' '.join(cmd)}\n\n"
                f"Details: {exc}",
            )
            return
        except Exception as exc:
            self._show_error("Dependency Installation Failed", str(exc))
            return
        finally:
            self._dependency_install_in_progress = False
            self._call_in_ui(self.login_button.configure, text=LOGIN_BUTTON_TEXT_DEFAULT)
            self._call_in_ui(self.auth_status_var.set, "Status: Not logged in")
            self._set_login_enabled(True)

        if self._ensure_dependencies(prompt=False):
            self._show_info(
                "Dependencies Installed",
                "Dependencies installed successfully. You can now log in to Google Earth Engine.",
            )
            self._call_in_ui(self.auth_status_var.set, "Status: Ready to log in")

    # ------------------------------ Callbacks -----------------------------

    def clear_outputs(self) -> None:
        if threading.current_thread() is threading.main_thread():
            self._clear_outputs_ui()
        else:
            self._call_in_ui(self._clear_outputs_ui)

    def _clear_outputs_ui(self) -> None:
        self.console_output_text.configure(state="normal")
        self.console_output_text.delete(1.0, tk.END)
        self.console_output_text.configure(state="disabled")

        self.tide_dates_text.configure(state="normal")
        self.tide_dates_text.delete(1.0, tk.END)
        self.tide_dates_text.configure(state="disabled")

        self._set_progress_ui(0)

    def authenticate(self) -> None:
        if not self._ensure_dependencies():
            return

        self.auth_status_var.set("Status: Authenticating...")
        self.login_button.configure(text=LOGIN_BUTTON_TEXT_WORKING)
        self._set_login_enabled(False)
        threading.Thread(target=self._authenticate_worker, daemon=True).start()

    def _authenticate_worker(self) -> None:
        if self._ee is None:
            self._show_error("Authentication Error", "Earth Engine dependency is not available.")
            self._set_login_enabled(True)
            return
        ee_module = self._ee

        original_input = builtins.input
        builtins.input = self._gui_input
        try:
            ee_module.Authenticate(auth_mode="notebook", force=True)
            ee_module.Initialize()
        except Exception as exc:
            self._show_error("Authentication Error", str(exc))
            self._call_in_ui(self.login_button.configure, text=LOGIN_BUTTON_TEXT_DEFAULT)
            self._call_in_ui(self.auth_status_var.set, "Status: Not logged in")
            self._set_login_enabled(True)
            return
        finally:
            builtins.input = original_input

        self._call_in_ui(self.login_button.configure, text=LOGIN_BUTTON_TEXT_AUTHENTICATED)
        self._call_in_ui(self.auth_status_var.set, "Status: Logged in")
        self._set_login_enabled(True)
        self._show_info("Success", "Authentication successful!")
        self._call_in_ui(self._enable_fields_with_defaults)

    def _gui_input(self, prompt: Optional[str] = None) -> str:
        if prompt:
            print(prompt)

        result: dict[str, Optional[str]] = {"value": None}
        done = threading.Event()

        def _ask() -> None:
            result["value"] = simpledialog.askstring(
                "Input Required",
                prompt or "Please enter the authentication code:",
            )
            done.set()

        self.root.after(0, _ask)
        done.wait()

        if result["value"] is None:
            raise RuntimeError("Authentication cancelled by user.")
        return result["value"]

    def on_execute(self) -> None:
        self._set_run_enabled(False)
        self.clear_outputs()
        self._set_progress(0)

        try:
            params = self._read_params_from_ui()
        except ValueError as exc:
            self._show_error("Invalid Input", str(exc))
            self._set_run_enabled(True)
            return

        valid, msg = _validate_params(params)
        if not valid:
            self._show_error("Invalid Parameters", msg)
            self._set_run_enabled(True)
            return

        threading.Thread(target=self._process_worker, args=(params,), daemon=True).start()

    def _process_worker(self, params: MapperParams) -> None:
        if not self._ensure_dependencies(prompt=False):
            self._set_run_enabled(True)
            return

        if self._ee is None:
            self._show_error("Processing Error", "Earth Engine dependency is not available.")
            self._set_run_enabled(True)
            return
        ee_module = self._ee

        try:
            self._set_progress(10)
            geometry_fc = ee_module.FeatureCollection(params.geometry_asset_id)
            export_region = geometry_fc.geometry()

            imgs = _build_image_collection(ee_module, geometry_fc, params)
            img_count = int(imgs.size().getInfo())

            if img_count == 0:
                print("No images were found that meet the conditions.")
                self._set_progress(100)
                self._call_in_ui(self._update_tide_dates, [])
                return

            self._set_progress(20)
            print(f"Found {img_count} images. Fetching metadata...")

            # Batch fetch all metadata in ONE API call instead of 4 calls per image
            imgs_list = imgs.toList(img_count)
            metadata_list = imgs_list.map(lambda img: ee_module.Feature(None, {
                "img_id": ee_module.Image(img).id(),
                "date": ee_module.Date(ee_module.Image(img).get("system:time_start")).format("YYYY-MM-dd"),
                "water": ee_module.Image(img).get("water_percentage"),
                "clouds": ee_module.Image(img).get("CLOUDY_PIXEL_PERCENTAGE"),
            }))
            all_metadata = ee_module.FeatureCollection(metadata_list).getInfo()

            self._set_progress(40)
            progress = 40.0
            progress_increment = 60.0 / img_count

            dates_with_low_tides: list[str] = []

            for i, feat in enumerate(all_metadata["features"]):
                props = feat["properties"]
                img_id = props["img_id"]
                img_date = props["date"]
                water_percentage = props["water"]
                cloud_coverage = props["clouds"]

                img = ee_module.Image(imgs_list.get(i))

                progress += progress_increment
                self._set_progress(progress)

                print()
                print(f"Image '{img_id}'")
                print(f"  Acquisition Date: {img_date}")
                print(f"  Cloud Coverage: {cloud_coverage:.2f}%")
                print(f"  Water Coverage: {water_percentage:.2f}%")
                print(f"  Image selected (< {params.max_water_percentage}% water)")
                print()

                dates_with_low_tides.append(img_date)

                _export_ndvi_and_rgb(
                    ee_module, img, img_date, export_region, params, params.tile,
                    is_first=(i == 0)
                )

            unique_dates = list(dict.fromkeys(dates_with_low_tides))
            if not unique_dates:
                print("No images were found that meet the conditions.")

            self._call_in_ui(self._update_tide_dates, unique_dates)
            self._set_progress(100)
        except Exception as exc:
            self._show_error("Processing Error", str(exc))
            self._set_progress(0)
        finally:
            self._set_run_enabled(True)

    # --------------------------- UI construction ---------------------------

    def _configure_layout(self) -> None:
        self.root.minsize(1100, 700)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        style = ttk.Style(self.root)
        style.configure("TButton", padding=(10, 6))
        style.configure("Primary.TButton", padding=(12, 8))

    def _create_widgets(self) -> None:
        self.main_frame = ttk.Frame(self.root, padding=(20, 16))
        self.main_frame.grid(row=0, column=0, sticky="nsew")
        self.main_frame.columnconfigure(0, weight=1)
        self.main_frame.columnconfigure(2, weight=1)
        self.main_frame.rowconfigure(0, weight=1)

        self.left_frame = ttk.Frame(self.main_frame)
        self.left_frame.grid(row=0, column=0, sticky="nsew")
        self.left_frame.columnconfigure(0, weight=1)

        separator = ttk.Separator(self.main_frame, orient="vertical")
        separator.grid(row=0, column=1, sticky="ns", padx=16)

        self.right_frame = ttk.Frame(self.main_frame)
        self.right_frame.grid(row=0, column=2, sticky="nsew")
        self.right_frame.columnconfigure(0, weight=1)
        self.right_frame.rowconfigure(0, weight=3)
        self.right_frame.rowconfigure(1, weight=2)

        auth_frame = ttk.Labelframe(self.left_frame, text="1) Authentication", padding=(12, 10))
        auth_frame.grid(row=0, column=0, sticky="ew")
        auth_frame.columnconfigure(0, weight=1)

        self.login_button = ttk.Button(auth_frame, text=LOGIN_BUTTON_TEXT_DEFAULT, command=self.authenticate)
        self.login_button.grid(row=0, column=0, sticky="w")

        self.auth_status_var = tk.StringVar(value="Status: Not logged in")
        auth_status = ttk.Label(auth_frame, textvariable=self.auth_status_var)
        auth_status.grid(row=1, column=0, pady=(8, 0), sticky="w")

        params_frame = ttk.Labelframe(self.left_frame, text="2) Parameters", padding=(12, 10))
        params_frame.grid(row=1, column=0, pady=(14, 0), sticky="nsew")
        params_frame.columnconfigure(0, weight=0)
        params_frame.columnconfigure(1, weight=0)
        params_frame.columnconfigure(2, weight=1)
        params_frame.columnconfigure(3, weight=0)
        params_frame.columnconfigure(4, weight=1)

        self._input_entries: list[tk.Entry] = []

        self.start_date_entry, self.end_date_entry = self._create_range_entries(
            params_frame,
            row=0,
            label="Date range (YYYY-MM-DD)",
            start_width=12,
            end_width=12,
        )
        self.cloudy_percentage_entry = self._create_entry(
            params_frame,
            row=1,
            label="Maximum cloud cover (%)",
            width=10,
            full_width=False,
        )
        self.max_water_percentage_entry = self._create_entry(
            params_frame,
            row=2,
            label="Maximum water cover (%)",
            width=10,
            full_width=False,
        )
        self.tile_entry = self._create_entry(
            params_frame,
            row=3,
            label="Sentinel-2 tile",
            width=12,
            full_width=False,
        )
        self.start_ndvi_entry, self.end_ndvi_entry = self._create_range_entries(
            params_frame,
            row=4,
            label="NDVI range",
            start_width=10,
            end_width=10,
        )
        self.geometry_entry = self._create_entry(
            params_frame,
            row=5,
            label="Geometry asset id (GEE)",
            width=44,
        )
        self.epsg_entry = self._create_entry(
            params_frame,
            row=6,
            label="CRS (EPSG)",
            width=10,
            full_width=False,
        )
        self.folder_name_entry = self._create_entry(
            params_frame,
            row=7,
            label="Google Drive folder name",
            width=44,
        )

        run_frame = ttk.Labelframe(self.left_frame, text="3) Run", padding=(12, 10))
        run_frame.grid(row=2, column=0, pady=(14, 0), sticky="ew")
        run_frame.columnconfigure(0, weight=1)

        self.run_button = ttk.Button(
            run_frame,
            text="Execute",
            command=self.on_execute,
            state="disabled",
            style="Primary.TButton",
        )
        self.run_button.grid(row=0, column=0, padx=4)

        self.progress_bar = ttk.Progressbar(run_frame, orient="horizontal", mode="determinate")
        self.progress_bar.grid(row=1, column=0, pady=(10, 0), sticky="ew")


        log_frame = ttk.Labelframe(self.right_frame, text="Output", padding=(12, 10))
        log_frame.grid(row=0, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.console_output_text = ScrolledText(
            log_frame,
            wrap=tk.WORD,
            width=60,
            height=14,
            state="disabled",
            font="TkFixedFont",
        )
        self.console_output_text.grid(row=0, column=0, sticky="nsew")

        dates_frame = ttk.Labelframe(
            self.right_frame,
            text="Processed Sentinel-2 imagery",
            padding=(12, 10),
        )
        dates_frame.grid(row=1, column=0, pady=(14, 0), sticky="nsew")
        dates_frame.columnconfigure(0, weight=1)
        dates_frame.rowconfigure(0, weight=1)

        self.tide_dates_text = ScrolledText(
            dates_frame,
            wrap=tk.WORD,
            width=60,
            height=8,
            state="disabled",
            font="TkFixedFont",
        )
        self.tide_dates_text.grid(row=0, column=0, sticky="nsew")

        bottom_bar = ttk.Frame(self.right_frame)
        bottom_bar.grid(row=2, column=0, pady=(14, 0), sticky="ew")
        bottom_bar.columnconfigure(0, weight=1)

        clear_button = ttk.Button(bottom_bar, text="Clear console", command=self.clear_outputs)
        clear_button.grid(row=0, column=0, sticky="e")

        about = ttk.Label(bottom_bar, text="Haro, S.  •  haropaez.sara@gmail.com", font="TkSmallCaptionFont")
        about.grid(row=1, column=0, pady=(8, 0), sticky="e")

        self._populate_defaults()
        self._disable_inputs()

    def _create_entry(
        self,
        parent: tk.Misc,
        *,
        row: int,
        label: str,
        width: int,
        full_width: bool = True,
    ) -> tk.Entry:
        label_widget = ttk.Label(parent, text=label)
        label_widget.grid(row=row, column=0, padx=(0, 10), pady=6, sticky="w")

        entry = ttk.Entry(parent, state="disabled", width=width)
        if full_width:
            entry.grid(row=row, column=2, columnspan=3, pady=6, sticky="ew")
        else:
            entry.grid(row=row, column=2, pady=6, sticky="w")

        self._input_entries.append(entry)
        return entry

    def _create_range_entries(
        self,
        parent: tk.Misc,
        *,
        row: int,
        label: str,
        start_width: int,
        end_width: int,
    ) -> tuple[tk.Entry, tk.Entry]:
        label_widget = ttk.Label(parent, text=label)
        label_widget.grid(row=row, column=0, padx=(0, 10), pady=6, sticky="w")

        start_entry = ttk.Entry(parent, state="disabled", width=start_width)
        start_entry.grid(row=row, column=2, padx=(0, 8), pady=6, sticky="ew")

        to_label = ttk.Label(parent, text="to")
        to_label.grid(row=row, column=3, padx=(0, 8), pady=6)

        end_entry = ttk.Entry(parent, state="disabled", width=end_width)
        end_entry.grid(row=row, column=4, pady=6, sticky="ew")

        self._input_entries.extend([start_entry, end_entry])
        return start_entry, end_entry

    def _populate_defaults(self) -> None:
        for entry in self._input_entries:
            entry.configure(state="normal")

        self._set_entry(self.start_date_entry, DEFAULT_ENTRY_VALUES["start_date"])
        self._set_entry(self.end_date_entry, DEFAULT_ENTRY_VALUES["end_date"])
        self._set_entry(self.cloudy_percentage_entry, DEFAULT_ENTRY_VALUES["cloudy_percentage"])
        self._set_entry(self.max_water_percentage_entry, DEFAULT_ENTRY_VALUES["max_water_percentage"])
        self._set_entry(self.tile_entry, DEFAULT_ENTRY_VALUES["tile"])
        self._set_entry(self.start_ndvi_entry, DEFAULT_ENTRY_VALUES["start_ndvi"])
        self._set_entry(self.end_ndvi_entry, DEFAULT_ENTRY_VALUES["end_ndvi"])
        self._set_entry(self.geometry_entry, DEFAULT_ENTRY_VALUES["geometry"])
        self._set_entry(self.epsg_entry, DEFAULT_ENTRY_VALUES["epsg"])
        self._set_entry(self.folder_name_entry, DEFAULT_ENTRY_VALUES["folder_name"])

        for entry in self._input_entries:
            entry.configure(state="disabled")

    def _disable_inputs(self) -> None:
        for entry in self._input_entries:
            entry.configure(state="disabled")

    def _enable_fields_with_defaults(self) -> None:
        for entry in self._input_entries:
            entry.configure(state="normal")

        self._set_entry_if_empty(self.start_date_entry, DEFAULT_ENTRY_VALUES["start_date"])
        self._set_entry_if_empty(self.end_date_entry, DEFAULT_ENTRY_VALUES["end_date"])
        self._set_entry_if_empty(self.cloudy_percentage_entry, DEFAULT_ENTRY_VALUES["cloudy_percentage"])
        self._set_entry_if_empty(self.max_water_percentage_entry, DEFAULT_ENTRY_VALUES["max_water_percentage"])
        self._set_entry_if_empty(self.tile_entry, DEFAULT_ENTRY_VALUES["tile"])
        self._set_entry_if_empty(self.start_ndvi_entry, DEFAULT_ENTRY_VALUES["start_ndvi"])
        self._set_entry_if_empty(self.end_ndvi_entry, DEFAULT_ENTRY_VALUES["end_ndvi"])
        self._set_entry_if_empty(self.geometry_entry, DEFAULT_ENTRY_VALUES["geometry"])
        self._set_entry_if_empty(self.epsg_entry, DEFAULT_ENTRY_VALUES["epsg"])
        self._set_entry_if_empty(self.folder_name_entry, DEFAULT_ENTRY_VALUES["folder_name"])

        self.run_button.configure(state="normal")

    def _set_entry(self, entry: tk.Entry, value: str) -> None:
        entry.delete(0, tk.END)
        entry.insert(0, value)

    def _set_entry_if_empty(self, entry: tk.Entry, value: str) -> None:
        if entry.get().strip():
            return
        self._set_entry(entry, value)

    def _update_tide_dates(self, dates: list[str]) -> None:
        self.tide_dates_text.configure(state="normal")
        self.tide_dates_text.delete(1.0, tk.END)
        self.tide_dates_text.insert(tk.END, "\n".join(dates))
        self.tide_dates_text.configure(state="disabled")

    def _read_params_from_ui(self) -> MapperParams:
        start_date = self.start_date_entry.get().strip()
        end_date = self.end_date_entry.get().strip()
        cloudy_percentage = _parse_int(self.cloudy_percentage_entry.get(), "Cloud percentage")
        max_water_percentage = _parse_int(self.max_water_percentage_entry.get(), "Max water percentage")
        tile = _parse_tile(self.tile_entry.get())
        start_ndvi = _parse_float(self.start_ndvi_entry.get(), "NDVI start")
        end_ndvi = _parse_float(self.end_ndvi_entry.get(), "NDVI end")
        geometry_asset_id = self.geometry_entry.get().strip()
        epsg = _parse_int(self.epsg_entry.get().strip(), "EPSG")
        drive_folder = self.folder_name_entry.get().strip()

        return MapperParams(
            start_date=start_date,
            end_date=end_date,
            cloudy_percentage=cloudy_percentage,
            max_water_percentage=max_water_percentage,
            tile=tile,
            start_ndvi=start_ndvi,
            end_ndvi=end_ndvi,
            geometry_asset_id=geometry_asset_id,
            epsg=epsg,
            drive_folder=drive_folder,
        )

    def _redirect_output(self) -> None:
        redirector = TextRedirector(self.console_output_text)
        sys.stdout = redirector
        sys.stderr = redirector


def run_app() -> None:
    BioIntertidalMapperApp().run()


if __name__ == "__main__":
    run_app()
