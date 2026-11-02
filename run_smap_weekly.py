import os
import sys
import time
import json
import datetime as dt

import ee
from google.oauth2 import service_account


# ==============================================================
# USER CONFIGURATION
# ==============================================================

GEE_PROJECT = os.environ.get("GEE_PROJECT")
GCS_BUCKET = os.environ.get("GCS_BUCKET")

# Folder-like prefix inside the Google Cloud Storage bucket
EXPORT_PREFIX = os.environ.get("EXPORT_PREFIX", "GEE/SMAP_RZSM")

NORMAL_START_YEAR = int(os.environ.get("NORMAL_START_YEAR", "2016"))
NORMAL_END_YEAR = int(os.environ.get("NORMAL_END_YEAR", "2025"))

EXPORT_SCALE = int(os.environ.get("EXPORT_SCALE", "9000"))

SMAP_COLLECTION = "NASA/SMAP/SPL4SMGP/008"
SMAP_BAND = "sm_rootzone"

INDIA_BOUNDARY = (
    "projects/qgis1-soilmoisture/assets/"
    "India_District_AllBoundary"
)

EXPORT_REGION = [
    67.5,
    6.0,
    98.5,
    38.5
]


# ==============================================================
# AUTHENTICATE EARTH ENGINE
# ==============================================================

def initialize_earth_engine():
    """
    Initialize Earth Engine using a service-account JSON stored
    in the GEE_SERVICE_ACCOUNT_JSON GitHub secret.
    """

    credentials_json = os.environ.get("GEE_SERVICE_ACCOUNT_JSON")

    if not credentials_json:
        raise ValueError(
            "GEE_SERVICE_ACCOUNT_JSON environment variable is missing."
        )

    if not GEE_PROJECT:
        raise ValueError(
            "GEE_PROJECT environment variable is missing."
        )

    service_account_info = json.loads(credentials_json)

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=[
            "https://www.googleapis.com/auth/earthengine",
            "https://www.googleapis.com/auth/cloud-platform"
        ]
    )

    ee.Initialize(
        credentials=credentials,
        project=GEE_PROJECT
    )

    print(f"Earth Engine initialized with project: {GEE_PROJECT}")


# ==============================================================
# DETERMINE PREVIOUS COMPLETED WEEK
# ==============================================================

def get_previous_completed_week():
    """
    Returns the previous completed Monday-to-Sunday period.

    If the workflow runs on Monday:
        start = previous Monday
        end inclusive = previous Sunday

    Earth Engine filterDate() excludes the end date, so the
    returned processing end date is Monday following Sunday.
    """

    today = dt.date.today()

    current_week_monday = today - dt.timedelta(
        days=today.weekday()
    )

    actual_start = current_week_monday - dt.timedelta(days=7)
    actual_end_inclusive = current_week_monday - dt.timedelta(days=1)

    # Exclusive end date required by Earth Engine filterDate()
    actual_end_exclusive = actual_end_inclusive + dt.timedelta(days=1)

    return (
        actual_start,
        actual_end_inclusive,
        actual_end_exclusive
    )


# ==============================================================
# CREATE YEAR-SPECIFIC DATE RANGE
# ==============================================================

def create_normal_period(year, actual_start, actual_end_inclusive):
    """
    Construct the corresponding historical date range for a
    normal year.

    Leap-day dates are adjusted to 28 February when necessary.
    """

    try:
        normal_start = dt.date(
            year,
            actual_start.month,
            actual_start.day
        )
    except ValueError:
        normal_start = dt.date(year, 2, 28)

    try:
        normal_end_inclusive = dt.date(
            year,
            actual_end_inclusive.month,
            actual_end_inclusive.day
        )
    except ValueError:
        normal_end_inclusive = dt.date(year, 2, 28)

    # Handle a week that crosses into the next calendar year
    if normal_end_inclusive < normal_start:
        normal_end_inclusive = normal_end_inclusive.replace(
            year=year + 1
        )

    normal_end_exclusive = (
        normal_end_inclusive + dt.timedelta(days=1)
    )

    return normal_start, normal_end_exclusive


# ==============================================================
# GET MEAN ROOT-ZONE SOIL MOISTURE
# ==============================================================

def get_mean_rzsm(start_date, end_date):
    collection = (
        ee.ImageCollection(SMAP_COLLECTION)
        .filterDate(
            start_date.isoformat(),
            end_date.isoformat()
        )
        .select(SMAP_BAND)
    )

    image_count = collection.size().getInfo()

    print(
        f"Images from {start_date} to "
        f"{end_date - dt.timedelta(days=1)}: "
        f"{image_count}"
    )

    if image_count == 0:
        return None

    return (
        collection
        .mean()
        .rename("RootZoneSM")
        .toFloat()
    )


# ==============================================================
# BUILD NORMAL IMAGE
# ==============================================================

def build_normal_image(actual_start, actual_end_inclusive):
    normal_images = []
    years_used = []

    for year in range(
        NORMAL_START_YEAR,
        NORMAL_END_YEAR + 1
    ):
        normal_start, normal_end_exclusive = (
            create_normal_period(
                year,
                actual_start,
                actual_end_inclusive
            )
        )

        yearly_image = get_mean_rzsm(
            normal_start,
            normal_end_exclusive
        )

        if yearly_image is not None:
            normal_images.append(yearly_image)
            years_used.append(year)

    if not normal_images:
        raise RuntimeError(
            "No historical SMAP images were available for "
            "the selected normal period."
        )

    print(f"Normal years used: {years_used}")

    normal = (
        ee.ImageCollection.fromImages(normal_images)
        .mean()
        .rename("RootZoneSM")
        .toFloat()
    )

    return normal, years_used


# ==============================================================
# EXPORT TO GOOGLE CLOUD STORAGE
# ==============================================================

def export_to_cloud_storage(
    image,
    description,
    file_name_prefix,
    region
):
    task = ee.batch.Export.image.toCloudStorage(
        image=image,
        description=description,
        bucket=GCS_BUCKET,
        fileNamePrefix=file_name_prefix,
        region=region,
        scale=EXPORT_SCALE,
        crs="EPSG:4326",
        maxPixels=1e13,
        fileFormat="GeoTIFF",
        formatOptions={
            "cloudOptimized": True,
            "noData": -9999
        }
    )

    task.start()

    print(f"Started export: {description}")
    print(f"Task ID: {task.id}")

    return task


# ==============================================================
# WAIT FOR EXPORT TASKS
# ==============================================================

def wait_for_tasks(tasks, check_interval=60):
    """
    Keep the GitHub Actions job running until all exports finish.
    """

    failed_tasks = []

    while tasks:
        running_tasks = []

        for task in tasks:
            status = task.status()
            state = status.get("state", "UNKNOWN")
            description = status.get(
                "description",
                task.id
            )

            print(f"{description}: {state}")

            if state in ["READY", "RUNNING"]:
                running_tasks.append(task)

            elif state == "COMPLETED":
                print(f"Completed: {description}")

            else:
                error_message = status.get(
                    "error_message",
                    "No error message returned"
                )

                print(
                    f"Failed: {description}. "
                    f"Reason: {error_message}"
                )

                failed_tasks.append(
                    {
                        "description": description,
                        "state": state,
                        "error": error_message
                    }
                )

        tasks = running_tasks

        if tasks:
            print(
                f"{len(tasks)} export task(s) still running. "
                f"Checking again in {check_interval} seconds."
            )
            time.sleep(check_interval)

    if failed_tasks:
        print("One or more exports failed:")

        for failure in failed_tasks:
            print(failure)

        raise RuntimeError(
            f"{len(failed_tasks)} Earth Engine export(s) failed."
        )

    print("All Earth Engine exports completed successfully.")


# ==============================================================
# MAIN PROCESS
# ==============================================================

def main():
    if not GCS_BUCKET:
        raise ValueError(
            "GCS_BUCKET environment variable is missing."
        )

    initialize_earth_engine()

    india = ee.FeatureCollection(INDIA_BOUNDARY)

    export_region = ee.Geometry.Rectangle(
        EXPORT_REGION,
        proj="EPSG:4326",
        geodesic=False
    )

    (
        actual_start,
        actual_end_inclusive,
        actual_end_exclusive
    ) = get_previous_completed_week()

    print("=" * 60)
    print("Weekly SMAP Root-Zone Soil Moisture Processing")
    print("=" * 60)
    print(f"Actual start: {actual_start}")
    print(f"Actual end:   {actual_end_inclusive}")
    print(
        f"Normal period: {NORMAL_START_YEAR}–"
        f"{NORMAL_END_YEAR}"
    )

    # ----------------------------------------------------------
    # Actual weekly mean
    # ----------------------------------------------------------

    actual = get_mean_rzsm(
        actual_start,
        actual_end_exclusive
    )

    if actual is None:
        raise RuntimeError(
            "No SMAP images were found for the actual week."
        )

    # ----------------------------------------------------------
    # Historical normal
    # ----------------------------------------------------------

    normal, years_used = build_normal_image(
        actual_start,
        actual_end_inclusive
    )

    # ----------------------------------------------------------
    # Anomaly and percentage departure
    # ----------------------------------------------------------

    anomaly = (
        actual
        .subtract(normal)
        .rename("Anomaly")
        .toFloat()
    )

    # Equivalent to the anomaly1 layer in the original script
    anomaly_scaled = (
        anomaly
        .multiply(100)
        .rename("Anomaly_x100")
        .toFloat()
    )

    # Avoid division by zero
    safe_normal = normal.updateMask(
        normal.neq(0)
    )

    departure = (
        anomaly
        .divide(safe_normal)
        .multiply(100)
        .rename("Departure")
        .toFloat()
    )

    # ----------------------------------------------------------
    # Clip to India
    # ----------------------------------------------------------

    actual = actual.clipToCollection(india).unmask(-9999)
    normal = normal.clipToCollection(india).unmask(-9999)
    anomaly = anomaly.clipToCollection(india).unmask(-9999)

    anomaly_scaled = (
        anomaly_scaled
        .clipToCollection(india)
        .unmask(-9999)
    )

    departure = (
        departure
        .clipToCollection(india)
        .unmask(-9999)
    )

    # ----------------------------------------------------------
    # Output naming
    # ----------------------------------------------------------

    period_label = (
        f"{actual_start:%Y%m%d}_"
        f"{actual_end_inclusive:%Y%m%d}"
    )

    normal_year_label = (
        f"{NORMAL_START_YEAR}_{NORMAL_END_YEAR}"
    )

    base_prefix = (
        f"{EXPORT_PREFIX}/{actual_start:%Y}/"
        f"{actual_start:%m}"
    )

    exports = [
        (
            actual,
            f"SMAP_RZSM_Actual_{period_label}",
            f"{base_prefix}/"
            f"SMAP_RZSM_Actual_{period_label}"
        ),
        (
            normal,
            f"SMAP_RZSM_Normal_"
            f"{normal_year_label}_{period_label}",
            f"{base_prefix}/"
            f"SMAP_RZSM_Normal_"
            f"{normal_year_label}_{period_label}"
        ),
        (
            anomaly,
            f"SMAP_RZSM_Anomaly_{period_label}",
            f"{base_prefix}/"
            f"SMAP_RZSM_Anomaly_{period_label}"
        ),
        (
            anomaly_scaled,
            f"SMAP_RZSM_Anomaly_x100_{period_label}",
            f"{base_prefix}/"
            f"SMAP_RZSM_Anomaly_x100_{period_label}"
        ),
        (
            departure,
            f"SMAP_RZSM_Departure_{period_label}",
            f"{base_prefix}/"
            f"SMAP_RZSM_Departure_{period_label}"
        )
    ]

    tasks = []

    for image, description, file_prefix in exports:
        task = export_to_cloud_storage(
            image=image,
            description=description,
            file_name_prefix=file_prefix,
            region=export_region
        )

        tasks.append(task)

    wait_for_tasks(tasks)

    print("=" * 60)
    print("Processing completed")
    print(f"Bucket: gs://{GCS_BUCKET}")
    print(f"Output prefix: {base_prefix}")
    print(f"Normal years used: {years_used}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print(f"Processing failed: {error}")
        sys.exit(1)
