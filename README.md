# SWMM Sewer Builder

**Professional QGIS plugin for sewer network design and EPA SWMM modelling.**

SWMM Sewer Builder provides a GIS-based workflow for designing gravity sewer networks, generating longitudinal profiles, estimating excavation costs, delineating drainage catchments and creating EPA SWMM models directly from QGIS.

![QGIS](https://img.shields.io/badge/QGIS-3.16%2B-green)
![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Python](https://img.shields.io/badge/Python-3.x-blue)
![EPA SWMM](https://img.shields.io/badge/EPA_SWMM-INP_export-lightgrey)
![License](https://img.shields.io/badge/license-GPL--3.0-orange)
Tutorial link on Youtube (https://youtu.be/NaxhriQfxME?si=aa2Z1cPkYbmm257r)

## Main features

- Interactive pipe and manhole creation inside QGIS
- Longitudinal profile generation and editing
- Manual GIS node insertion from the Profile Editor
- Hydraulic checks with GR80 filling ratio and velocity validation
- Excavation and pipe cost estimation
- Drainage catchment and subcatchment generation
- Design storm definition
- EPA SWMM `.inp` model generation
- Storage nodes, pumps, weirs and orifices
- SWMM simulation workflow and result mapping
- GeoPackage and CSV export with English field names
- CAD-oriented profile/export workflow

## Typical workflow

1. Load the terrain model and existing GIS layers.
2. Draw or import the proposed sewer pipe alignment.
3. Generate manholes and pipe segments.
4. Open the Profile Editor and define the hydraulic profile.
5. Add manual GIS nodes where needed.
6. Delineate drainage catchments and define the design storm.
7. Generate the EPA SWMM model.
8. Run the simulation and review results in QGIS.
9. Export GeoPackage, CSV and project deliverables.

## Installation from ZIP

1. Download the plugin ZIP from the `release/` folder or from the GitHub Releases page.
2. Open QGIS.
3. Go to **Plugins → Manage and Install Plugins → Install from ZIP**.
4. Select the plugin ZIP file.
5. Enable **SWMM Sewer Builder** from the plugin manager.

## Repository structure

```text
SewerBuilder/
├── SewerSWMMBuilder/        # QGIS plugin package
├── docs/                    # User and developer documentation
├── examples/                # Example projects and sample data placeholders
├── screenshots/             # Screenshots and presentation images
├── release/                 # Installable QGIS plugin ZIP
├── .github/                 # GitHub templates and workflows
├── README.md
├── LICENSE
├── CHANGELOG.md
├── CONTRIBUTING.md
└── SECURITY.md
```

## Documentation

- [Installation](docs/installation.md)
- [Quick start](docs/quick_start.md)
- [Profile Editor](docs/profile_editor.md)
- [SWMM workflow](docs/swmm_workflow.md)
- [Cost estimation](docs/cost_estimation.md)

## License

This project is distributed under the GNU General Public License v3.0.

## Author

Developed by **Marco Buoso**.
