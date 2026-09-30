// ===========================================================================
//  HttpClient.h - minimal HTTP/1.1 client over POSIX sockets
//
//  Why not libcurl?  The scanner only ever talks to 127.0.0.1, needs GET and
//  POST with JSON or multipart bodies, and must build on a machine with nothing
//  but build-essential installed.  A ~250 line socket client removes a system
//  dependency and keeps the whole integration auditable.
//
//  Guarantees:
//  * connects with a timeout, so a dead API cannot hang the scan thread
//  * reads exactly Content-Length bytes (chunked encoding is decoded too)
//  * never throws; failures are reported through the return value
//  * percent-encodes form field values, so a barcode can never break the body
// ===========================================================================
#ifndef SIDB_API_HTTP_CLIENT_H
#define SIDB_API_HTTP_CLIENT_H

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace sidb::http {

struct Response {
    bool ok = false;              ///< transport succeeded (any status code)
    int status = 0;               ///< HTTP status, 0 when the transport failed
    std::string body;
    std::map<std::string, std::string> headers;
    std::string error;            ///< transport error message, empty on success
    double duration_ms = 0.0;

    /// The exchange completed (any status code).  Distinguishes "the service
    /// answered" from "the service could not be reached".
    bool transport_ok() const { return ok; }
    bool status_ok() const { return status >= 200 && status < 300; }
    bool json_contains(const std::string& needle) const { return body.find(needle) != std::string::npos; }
};

/// One part of a multipart/form-data body.
struct MultipartField {
    std::string name;
    std::string value;
    bool is_file = false;
    std::string filename;
    std::string content_type;
    std::string file_bytes;
};

class HttpClient {
public:
    HttpClient(std::string host, int port, int timeout_ms);

    /// GET `path` (e.g. "/api/health").
    Response get(const std::string& path) const;

    /// POST a JSON body with `Content-Type: application/json`.
    Response post_json(const std::string& path, const std::string& json) const;

    /// POST a multipart/form-data body (used to publish camera frames).
    Response post_multipart(const std::string& path,
                            const std::vector<MultipartField>& fields) const;

    /// POST with an explicit `Authorization: Bearer <token>` header.
    Response post_json_auth(const std::string& path, const std::string& json,
                            const std::string& bearer_token) const;

    const std::string& host() const { return host_; }
    int port() const { return port_; }
    void set_port(int port) { port_ = port; }
    void set_timeout_ms(int timeout_ms) { timeout_ms_ = timeout_ms; }

    /// True when something is listening on host:port right now.
    static bool is_reachable(const std::string& host, int port, int timeout_ms = 500);

    /// Percent-encode a value for use in a URL path segment or query string.
    static std::string url_encode(const std::string& value);

    /// Very small helper for the JSON bodies the scanner sends.  It only
    /// escapes what a barcode or a device label can contain - it is not a
    /// general purpose JSON writer and does not pretend to be.
    static std::string json_escape(const std::string& value);

private:
    Response request(const std::string& method, const std::string& path, const std::string& body,
                     const std::string& content_type, const std::string& bearer_token) const;

    std::string host_;
    int port_;
    int timeout_ms_;
};

}  // namespace sidb::http

#endif  // SIDB_API_HTTP_CLIENT_H
