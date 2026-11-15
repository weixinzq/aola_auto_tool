// AMF3 receive decoder. Keep the Python reader as the compatibility reference.
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

namespace {
struct Object {
    PyObject* value;
    explicit Object(PyObject* value) : value(value) {}
    ~Object() { Py_XDECREF(value); }
    Object(const Object&) = delete;
    Object& operator=(const Object&) = delete;
    PyObject* release() { PyObject* result = value; value = nullptr; return result; }
};

class Reader {
    const unsigned char* data;
    Py_ssize_t size, pos = 0;
    unsigned depth = 0;
    Object strings{PyList_New(0)}, objects{PyList_New(0)}, traits{PyList_New(0)};

    bool available(Py_ssize_t count) {
        if (count > size - pos) {
            PyErr_SetString(PyExc_EOFError, "AMF3 EOF");
            return false;
        }
        return true;
    }
    bool u29(uint32_t& value) {
        value = 0;
        for (int i = 0; i < 4; ++i) {
            if (!available(1)) return false;
            unsigned char byte = data[pos++];
            if (i == 3) { value = (value << 8) | byte; return true; }
            value = (value << 7) | (byte & 0x7f);
            if (!(byte & 0x80)) return true;
        }
        return true;
    }
    PyObject* reference(PyObject* table, uint32_t index) {
        PyObject* value = PyList_GetItem(table, index);
        return value ? Py_NewRef(value) : nullptr;
    }
    PyObject* string() {
        uint32_t ref;
        if (!u29(ref)) return nullptr;
        if (!(ref & 1)) return reference(strings.value, ref >> 1);
        Py_ssize_t length = ref >> 1;
        if (!available(length)) return nullptr;
        if (!length) return PyUnicode_FromString("");
        PyObject* text = PyUnicode_DecodeUTF8(reinterpret_cast<const char*>(data + pos), length, "strict");
        if (!text && PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {
            PyErr_Clear();
            text = PyUnicode_DecodeLatin1(reinterpret_cast<const char*>(data + pos), length, "strict");
        }
        Object result(text);
        pos += length;
        if (!result.value || PyList_Append(strings.value, result.value) < 0) return nullptr;
        return result.release();
    }
    PyObject* array(uint32_t ref) {
        if (!(ref & 1)) return reference(objects.value, ref >> 1);
        Object key(string());
        if (!key.value) return nullptr;
        bool dense = PyUnicode_GET_LENGTH(key.value) == 0;
        Object result(dense ? PyList_New(0) : PyDict_New());
        if (!result.value || PyList_Append(objects.value, result.value) < 0) return nullptr;
        if (!dense) {
            while (PyUnicode_GET_LENGTH(key.value)) {
                Object value(object());
                if (!value.value || PyDict_SetItem(result.value, key.value, value.value) < 0) return nullptr;
                Py_DECREF(key.release());
                key.value = string();
                if (!key.value) return nullptr;
            }
        }
        for (uint32_t i = 0; i < (ref >> 1); ++i) {
            Object value(object());
            if (!value.value) return nullptr;
            if (dense) {
                if (PyList_Append(result.value, value.value) < 0) return nullptr;
            } else {
                Object index(PyLong_FromUnsignedLong(i));
                if (!index.value || PyDict_SetItem(result.value, index.value, value.value) < 0) return nullptr;
            }
        }
        return result.release();
    }
    PyObject* script_object(uint32_t ref) {
        if (!(ref & 1)) return reference(objects.value, ref >> 1);
        Object description(nullptr);
        if ((ref & 3) == 1) {
            description.value = reference(traits.value, ref >> 2);
        } else {
            Object class_name(string()), properties(PyList_New(0)), flags(PyLong_FromUnsignedLong(ref & 12));
            if (!class_name.value || !properties.value || !flags.value) return nullptr;
            description.value = PyTuple_Pack(2, flags.value, properties.value);
            if (!description.value || PyList_Append(traits.value, description.value) < 0) return nullptr;
            for (uint32_t i = 0; i < (ref >> 4); ++i) {
                Object name(string());
                if (!name.value || PyList_Append(properties.value, name.value) < 0) return nullptr;
            }
        }
        if (!description.value) return nullptr;
        long flags = PyLong_AsLong(PyTuple_GET_ITEM(description.value, 0));
        PyObject* properties = PyTuple_GET_ITEM(description.value, 1);
        Object result(PyDict_New());
        if (!result.value || PyList_Append(objects.value, result.value) < 0) return nullptr;
        if (flags & 4) {
            Object value(object());
            if (!value.value) return nullptr;
            int truth = PyObject_IsTrue(value.value);
            if (truth < 0) return nullptr;
            // Match the existing reader's externalizable value-or-empty-map behavior.
            return truth ? value.release() : PyDict_New();
        }
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(properties); ++i) {
            Object value(object());
            if (!value.value || PyDict_SetItem(result.value, PyList_GET_ITEM(properties, i), value.value) < 0) return nullptr;
        }
        if (flags & 8) {
            while (true) {
                Object name(string());
                if (!name.value) return nullptr;
                if (!PyUnicode_GET_LENGTH(name.value)) break;
                Object value(object());
                if (!value.value || PyDict_SetItem(result.value, name.value, value.value) < 0) return nullptr;
            }
        }
        return result.release();
    }
    PyObject* value() {
        if (!available(1)) return nullptr;
        unsigned char marker = data[pos++];
        uint32_t ref;
        switch (marker) {
        case 0: case 1: return Py_NewRef(Py_None);
        case 2: return Py_NewRef(Py_False);
        case 3: return Py_NewRef(Py_True);
        case 4:
            if (!u29(ref)) return nullptr;
            return PyLong_FromLong(ref >= 0x10000000 ? static_cast<long>(ref) - 0x20000000L : ref);
        case 5: {
            if (!available(8)) return nullptr;
            double number = PyFloat_Unpack8(reinterpret_cast<const char*>(data + pos), 0);
            pos += 8;
            return PyErr_Occurred() ? nullptr : PyFloat_FromDouble(number);
        }
        case 6: return string();
        case 9:
            if (!u29(ref)) return nullptr;
            return array(ref);
        case 10:
            if (!u29(ref)) return nullptr;
            return script_object(ref);
        case 12: {
            if (!u29(ref)) return nullptr;
            if (!(ref & 1)) return reference(objects.value, ref >> 1);
            Py_ssize_t length = ref >> 1;
            if (!available(length)) return nullptr;
            Object bytes(PyBytes_FromStringAndSize(reinterpret_cast<const char*>(data + pos), length));
            pos += length;
            if (!bytes.value || PyList_Append(objects.value, bytes.value) < 0) return nullptr;
            return bytes.release();
        }
        default:
            PyErr_Format(PyExc_ValueError, "Unknown AMF3 type byte: %d", marker);
            return nullptr;
        }
    }
public:
    Reader(const char* data, Py_ssize_t size) : data(reinterpret_cast<const unsigned char*>(data)), size(size) {}
    bool ready() const { return strings.value && objects.value && traits.value; }
    PyObject* object() {
        // C recursion checks vary between Python builds; bound the native stack too.
        if (depth >= 512) {
            PyErr_SetString(PyExc_RecursionError, "AMF3 nesting exceeds 512 levels");
            return nullptr;
        }
        if (Py_EnterRecursiveCall(" while decoding AMF3")) return nullptr;
        ++depth;
        PyObject* result = value();
        --depth;
        Py_LeaveRecursiveCall();
        return result;
    }
};

PyObject* decode(PyObject*, PyObject* argument) {
    char* data;
    Py_ssize_t size;
    if (PyBytes_AsStringAndSize(argument, &data, &size) < 0) return nullptr;
    Reader reader(data, size);
    return reader.ready() ? reader.object() : nullptr;
}
PyMethodDef methods[] = {
    {"decode", decode, METH_O, "Decode one AMF3 value from bytes."},
    {nullptr, nullptr, 0, nullptr}
};
PyModuleDef module = {PyModuleDef_HEAD_INIT, "_amf3_native", nullptr, -1, methods, nullptr, nullptr, nullptr, nullptr};
}

PyMODINIT_FUNC PyInit__amf3_native() { return PyModule_Create(&module); }
