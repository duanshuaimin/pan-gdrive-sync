from setuptools import setup, find_packages

setup(
    name="pan-gdrive-sync",
    version="1.0.0",
    description="High-performance bidirectional file transfer between Baidu Netdisk and Google Drive",
    author="duansm",
    packages=find_packages(),
    include_package_data=True,
    package_data={
        "pangdrive": ["web/static/*"],
    },
    install_requires=[
        "requests>=2.25.0",
        "rich>=12.0.0",
        "click>=8.0.0",
        "urllib3>=1.26.0",
        "cryptography>=3.4.0",
        "flask>=2.0.0",
    ],
    entry_points={
        "console_scripts": [
            "pan-gdrive-sync=pangdrive.cli:main",
            "pgsync=pangdrive.cli:main",
        ],
    },
    python_requires=">=3.8",
)
