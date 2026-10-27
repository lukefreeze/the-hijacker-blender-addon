#pragma once
// Minimal stand-in for Blender's audaspace ISound.h, used when building
// hijacker_engine against a Blender version we don't have real Blender
// C++ source headers for (see .github/workflows/build.yml, which uses
// this same stub approach for all its CI builds). hijacker_processor.cpp
// only ever needs aud::sample_t and opaque ISound/IReader types -- it
// never calls any ISound/IReader methods -- so these empty stand-ins are
// enough to satisfy the forward declarations in hijacker_processor.h.
namespace aud {
    typedef float sample_t;
    class ISound {};
    class IReader {};
}
