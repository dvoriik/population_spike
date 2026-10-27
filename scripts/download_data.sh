#!/usr/bin/env bash
# Download every input used by the scripts into ./data (about 300 MB).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/dem
cd data

echo "1/5  ČSÚ population grid, Census 2021 (1 km)"
curl -fL -o grid_2021.zip \
  "https://geodata.csu.gov.cz/as/data/distribuce/Hosted/Gridy_SLDB2021/FeatureServer/0/gpkg.zip"
unzip -o -q grid_2021.zip && rm grid_2021.zip

echo "2/5  Eurostat GISCO: country borders, municipalities (LAU 2021), regions (NUTS 2021)"
curl -fL -o countries.geojson \
  "https://gisco-services.ec.europa.eu/distribution/v2/countries/geojson/CNTR_RG_01M_2020_3035.geojson"
curl -fL -o lau_2021.geojson \
  "https://gisco-services.ec.europa.eu/distribution/v2/lau/geojson/LAU_RG_01M_2021_3035.geojson"
curl -fL -o nuts3_2021.geojson \
  "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_01M_2021_3035_LEVL_3.geojson"

echo "3/5  ČSÚ population of municipalities, 1 Jan 2025"
curl -fL -o obce_2025.xlsx \
  "https://csu.gov.cz/docs/107508/ebed5ef3-dca1-baf2-c0d3-824b0893086f/1300722503.xlsx?version=1.0"

echo "4/5  Copernicus DEM GLO-90 tiles covering Czechia (28 tiles)"
for la in 48 49 50 51; do
  for lo in 12 13 14 15 16 17 18; do
    n="Copernicus_DSM_COG_30_N${la}_00_E0${lo}_00_DEM"
    [ -f "dem/$n.tif" ] || curl -fsSL -o "dem/$n.tif" \
      "https://copernicus-dem-90m.s3.amazonaws.com/$n/$n.tif"
  done
done

echo "5/5  done"
ls -lh
