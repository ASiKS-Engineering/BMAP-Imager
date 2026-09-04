cmake_minimum_required(VERSION 3.20)
project(bmapflash LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(bmapflash src/main.cpp)
target_compile_definitions(bmapflash PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)

target_compile_options(bmapflash PRIVATE -Wall -Wextra -Wpedantic)
target_link_options(bmapflash PRIVATE -static -static-libgcc -static-libstdc++)
target_link_libraries(bmapflash PRIVATE advapi32 setupapi)

if(MINGW)
  set_target_properties(bmapflash PROPERTIES
    WIN32_EXECUTABLE FALSE
    LINK_FLAGS "-municode"
  )
endif()
