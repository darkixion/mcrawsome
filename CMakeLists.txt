cmake_minimum_required(VERSION 3.20)
project(mcrawsome_raw LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)
find_package(pybind11 CONFIG REQUIRED)
find_package(Threads REQUIRED)
set(MCD native/motioncam-decoder)
pybind11_add_module(_raw native/raw_module.cpp native/ljpeg.cpp native/look.cpp ${MCD}/lib/RawData.cpp ${MCD}/lib/RawData_Legacy.cpp)
target_include_directories(_raw PRIVATE ${MCD}/lib/include ${MCD}/thirdparty native)
target_compile_options(_raw PRIVATE -O3 -ffp-contract=off)
target_link_libraries(_raw PRIVATE Threads::Threads)
install(TARGETS _raw DESTINATION mcrawsome)

install(DIRECTORY packaging/ DESTINATION mcrawsome/packaging
        FILES_MATCHING PATTERN "*.txt" PATTERN "*.md" PATTERN "*.toml" PATTERN "*.json"
        PATTERN "mcrawsome.desktop" PATTERN "mcrawsome.svg" PATTERN "mcrawsome-mime.xml" PATTERN "mcrawsome_menu.py" PATTERN "mcrawsome.metainfo.xml")

# Physical export remains available on machines without Linux FUSE 3 development files.
find_path(FUSE3_INCLUDE_DIR fuse.h PATH_SUFFIXES fuse3)
find_library(FUSE3_LIBRARY fuse3)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND FUSE3_INCLUDE_DIR AND FUSE3_LIBRARY)
    pybind11_add_module(_fuse native/fuse_module.cpp)
    target_include_directories(_fuse PRIVATE ${FUSE3_INCLUDE_DIR})
    target_compile_definitions(_fuse PRIVATE _FILE_OFFSET_BITS=64)
    target_link_libraries(_fuse PRIVATE ${FUSE3_LIBRARY})
    install(TARGETS _fuse DESTINATION mcrawsome)
endif()
