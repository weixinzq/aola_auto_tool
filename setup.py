from setuptools import Extension, setup


setup(ext_modules=[Extension(
    "src.protocol._amf3_native",
    sources=["src/protocol/_amf3_native.cpp"],
    language="c++",
    optional=True,
)])
