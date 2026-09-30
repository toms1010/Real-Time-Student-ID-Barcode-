// ===========================================================================
//  Testing.h - a 100-line test harness registered with CTest
//
//  Why not GoogleTest/Catch2?  Both would work, but each adds a FetchContent
//  download to the build.  The suite here needs assertions, fixtures and a
//  non-zero exit code - about a hundred lines - and keeping it in-tree means
//  `ctest` works on a machine with no network access.
//
//      TEST_CASE(name) { CHECK(cond); CHECK_EQ(a, b); }
//
//  Every check prints a file:line on failure and the process exits 1.
// ===========================================================================
#ifndef SIDB_TESTS_TESTING_H
#define SIDB_TESTS_TESTING_H

#include <cmath>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace sidb::test {

struct Registry {
    struct Entry {
        std::string name;
        std::function<void()> body;
    };
    static std::vector<Entry>& entries() {
        static std::vector<Entry> registry;
        return registry;
    }
    static int& failures() {
        static int count = 0;
        return count;
    }
    static std::string& current() {
        static std::string name;
        return name;
    }
};

struct Registrar {
    Registrar(const std::string& name, std::function<void()> body) {
        Registry::entries().push_back({name, std::move(body)});
    }
};

template <typename T>
std::string describe(const T& value) {
    std::ostringstream out;
    out << value;
    return out.str();
}

inline std::string describe(const std::string& value) { return "\"" + value + "\""; }
inline std::string describe(bool value) { return value ? "true" : "false"; }

inline void report(const char* file, int line, const std::string& expression,
                   const std::string& detail) {
    ++Registry::failures();
    std::cerr << "  FAIL  " << file << ":" << line << "\n"
              << "        " << expression << "\n";
    if (!detail.empty()) std::cerr << "        " << detail << "\n";
}

}  // namespace sidb::test

#define SIDB_TEST_CONCAT_INNER(a, b) a##b
#define SIDB_TEST_CONCAT(a, b) SIDB_TEST_CONCAT_INNER(a, b)

#define TEST_CASE(name)                                                                \
    static void SIDB_TEST_CONCAT(sidb_test_, __LINE__)();                             \
    static ::sidb::test::Registrar SIDB_TEST_CONCAT(sidb_registrar_, __LINE__)(        \
        name, &SIDB_TEST_CONCAT(sidb_test_, __LINE__));                                \
    static void SIDB_TEST_CONCAT(sidb_test_, __LINE__)()

#define CHECK(expr)                                                                   \
    do {                                                                              \
        if (!(expr)) {                                                                \
            ::sidb::test::report(__FILE__, __LINE__, "CHECK(" #expr ")", {});          \
        }                                                                             \
    } while (false)

#define CHECK_MSG(expr, detail)                                                        \
    do {                                                                              \
        if (!(expr)) {                                                                \
            ::sidb::test::report(__FILE__, __LINE__, "CHECK(" #expr ")", (detail));    \
        }                                                                             \
    } while (false)

#define CHECK_EQ(actual, expected)                                                    \
    do {                                                                              \
        const auto& sidb_actual = (actual);                                           \
        const auto& sidb_expected = (expected);                                       \
        if (!(sidb_actual == sidb_expected)) {                                        \
            ::sidb::test::report(__FILE__, __LINE__,                                  \
                                 "CHECK_EQ(" #actual ", " #expected ")",              \
                                 "actual   = " + ::sidb::test::describe(sidb_actual) +\
                                     "\n        expected = " +                        \
                                     ::sidb::test::describe(sidb_expected));           \
        }                                                                             \
    } while (false)

#define CHECK_NEAR(actual, expected, tolerance)                                       \
    do {                                                                              \
        const double sidb_actual = static_cast<double>(actual);                        \
        const double sidb_expected = static_cast<double>(expected);                    \
        if (std::fabs(sidb_actual - sidb_expected) > static_cast<double>(tolerance)) { \
            ::sidb::test::report(__FILE__, __LINE__,                                  \
                                 "CHECK_NEAR(" #actual ", " #expected ")",            \
                                 "actual   = " + std::to_string(sidb_actual) +         \
                                     "\n        expected = " + std::to_string(sidb_expected) + \
                                     " +/- " + std::to_string(static_cast<double>(tolerance))); \
        }                                                                             \
    } while (false)

#define CHECK_NOT_NULL(pointer)                                                       \
    do {                                                                              \
        if ((pointer) == nullptr) {                                                   \
            ::sidb::test::report(__FILE__, __LINE__, "CHECK_NOT_NULL(" #pointer ")",   \
                                 "pointer is null");                                  \
        }                                                                             \
    } while (false)

#define RUN_ALL_TESTS()                                                               \
    do {                                                                              \
        int sidb_run = 0;                                                             \
        for (const auto& entry : ::sidb::test::Registry::entries()) {                 \
            const int before = ::sidb::test::Registry::failures();                    \
            std::cout << "[ RUN      ] " << entry.name << "\n";                        \
            entry.body();                                                             \
            const int added = ::sidb::test::Registry::failures() - before;            \
            if (added == 0) {                                                         \
                std::cout << "[       OK ] " << entry.name << "\n";                   \
            } else {                                                                  \
                std::cout << "[  FAILED  ] " << entry.name << " (" << added             \
                          << " check(s))\n";                                          \
                ++sidb_run;                                                           \
            }                                                                         \
        }                                                                             \
        std::cout << "\n"                                                             \
                  << ::sidb::test::Registry::entries().size() - sidb_run << " of "    \
                  << ::sidb::test::Registry::entries().size() << " test(s) passed\n";  \
        return ::sidb::test::Registry::failures() == 0 ? 0 : 1;                       \
    } while (false)

#endif  // SIDB_TESTS_TESTING_H
