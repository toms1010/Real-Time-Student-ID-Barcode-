// ===========================================================================
//  HttpClient.cpp
// ===========================================================================
#include "api/HttpClient.h"

#include <arpa/inet.h>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <iomanip>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sstream>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include "utils/Logger.hpp"

namespace sidb::http {

namespace {

constexpr const char* kComponent = "http";

double now_ms() {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration<double, std::milli>(clock::now().time_since_epoch()).count();
}

std::string to_lower(std::string value) {
    for (char& c : value) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return value;
}

std::string trim(const std::string& value) {
    const auto begin = value.find_first_not_of(" \t\r\n");
    if (begin == std::string::npos) return {};
    const auto end = value.find_last_not_of(" \t\r\n");
    return value.substr(begin, end - begin + 1);
}

/// RAII socket so no early return can leak a descriptor.
class Socket {
public:
    explicit Socket(int fd) : fd_(fd) {}
    ~Socket() { close(); }
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;
    int get() const { return fd_; }
    void close() {
        if (fd_ >= 0) {
            ::close(fd_);
            fd_ = -1;
        }
    }

private:
    int fd_;
};

/// Connect with a timeout.  Returns -1 and fills `error` on failure.
int connect_with_timeout(const std::string& host, int port, int timeout_ms, std::string& error) {
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* results = nullptr;
    const std::string service = std::to_string(port);
    if (::getaddrinfo(host.c_str(), service.c_str(), &hints, &results) != 0 || results == nullptr) {
        error = "cannot resolve " + host;
        return -1;
    }

    int fd = -1;
    for (addrinfo* candidate = results; candidate != nullptr; candidate = candidate->ai_next) {
        fd = ::socket(candidate->ai_family, candidate->ai_socktype, candidate->ai_protocol);
        if (fd < 0) continue;

        const int flags = ::fcntl(fd, F_GETFL, 0);
        ::fcntl(fd, F_SETFL, flags | O_NONBLOCK);
        int status = ::connect(fd, candidate->ai_addr, candidate->ai_addrlen);
        if (status < 0 && errno == EINPROGRESS) {
            pollfd pfd{};
            pfd.fd = fd;
            pfd.events = POLLOUT;
            status = ::poll(&pfd, 1, timeout_ms);
            if (status > 0) {
                int error_code = 0;
                socklen_t length = sizeof(error_code);
                ::getsockopt(fd, SOL_SOCKET, SO_ERROR, &error_code, &length);
                status = error_code == 0 ? 0 : -1;
                if (error_code != 0) error = std::strerror(error_code);
            } else if (status == 0) {
                error = "connection timed out";
                status = -1;
            }
        }
        if (status == 0) {
            ::fcntl(fd, F_SETFL, flags);  // back to blocking
            int one = 1;
            ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
            timeval tv{};
            tv.tv_sec = timeout_ms / 1000;
            tv.tv_usec = (timeout_ms % 1000) * 1000;
            ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
            ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
            ::freeaddrinfo(results);
            return fd;
        }
        error = std::string("connect failed: ") + std::strerror(errno);
        ::close(fd);
        fd = -1;
    }
    ::freeaddrinfo(results);
    if (error.empty()) error = "connection refused";
    return -1;
}

bool send_all(int fd, const std::string& data) {
    std::size_t sent = 0;
    while (sent < data.size()) {
        const ssize_t written = ::send(fd, data.data() + sent, data.size() - sent, MSG_NOSIGNAL);
        if (written <= 0) {
            if (written < 0 && errno == EINTR) continue;
            return false;
        }
        sent += static_cast<std::size_t>(written);
    }
    return true;
}

}  // namespace

HttpClient::HttpClient(std::string host, int port, int timeout_ms)
    : host_(std::move(host)), port_(port), timeout_ms_(timeout_ms) {}

std::string HttpClient::url_encode(const std::string& value) {
    std::ostringstream out;
    out << std::uppercase << std::hex;
    for (const unsigned char c : value) {
        if (std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
            out << static_cast<char>(c);
        } else {
            out << '%' << std::setw(2) << std::setfill('0') << static_cast<int>(c);
        }
    }
    return out.str();
}

std::string HttpClient::json_escape(const std::string& value) {
    std::string out;
    out.reserve(value.size() + 8);
    for (const char c : value) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    std::ostringstream escape;
                    escape << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                           << static_cast<int>(static_cast<unsigned char>(c));
                    out += escape.str();
                } else {
                    out += c;
                }
        }
    }
    return out;
}

Response HttpClient::request(const std::string& method, const std::string& path,
                             const std::string& body, const std::string& content_type,
                             const std::string& bearer_token) const {
    Response response;
    const double started = now_ms();

    std::string error;
    Socket socket(connect_with_timeout(host_, port_, timeout_ms_, error));
    if (socket.get() < 0) {
        response.error = error;
        response.duration_ms = now_ms() - started;
        return response;
    }

    std::ostringstream request;
    request << method << ' ' << (path.empty() ? "/" : path) << " HTTP/1.1\r\n"
            << "Host: " << host_ << ':' << port_ << "\r\n"
            << "User-Agent: StudentIdScanner/1.0\r\n"
            << "Connection: close\r\n"
            << "Accept: application/json\r\n";
    if (!content_type.empty()) {
        request << "Content-Type: " << content_type << "\r\n"
                << "Content-Length: " << body.size() << "\r\n";
    }
    if (!bearer_token.empty()) {
        request << "Authorization: Bearer " << bearer_token << "\r\n";
    }
    request << "\r\n";
    if (!body.empty()) request << body;

    if (!send_all(socket.get(), request.str())) {
        response.error = "failed to send the request";
        response.duration_ms = now_ms() - started;
        return response;
    }

    std::string raw;
    char buffer[8192];
    while (true) {
        const ssize_t received = ::recv(socket.get(), buffer, sizeof(buffer), 0);
        if (received == 0) break;
        if (received < 0) {
            if (errno == EINTR) continue;
            response.error = std::string("failed to read the response: ") + std::strerror(errno);
            break;
        }
        raw.append(buffer, static_cast<std::size_t>(received));
        if (raw.size() > 64u * 1024u * 1024u) {  // runaway body guard
            response.error = "response exceeded 64 MiB";
            break;
        }
    }
    response.duration_ms = now_ms() - started;
    response.ok = response.error.empty();
    if (!response.ok) return response;

    // --- status line + headers ---
    const std::size_t header_end = raw.find("\r\n\r\n");
    if (header_end == std::string::npos) {
        response.error = "malformed response (no header terminator)";
        return response;
    }
    const std::string headers = raw.substr(0, header_end);
    response.body = raw.substr(header_end + 4);

    std::istringstream header_stream(headers);
    std::string status_line;
    std::getline(header_stream, status_line);
    {
        std::istringstream status_parser(status_line);
        std::string version;
        status_parser >> version >> response.status;
    }
    std::string line;
    bool chunked = false;
    long long content_length = -1;
    while (std::getline(header_stream, line)) {
        const std::size_t colon = line.find(':');
        if (colon == std::string::npos) continue;
        const std::string key = to_lower(trim(line.substr(0, colon)));
        const std::string value = trim(line.substr(colon + 1));
        response.headers[key] = value;
        if (key == "transfer-encoding" && to_lower(value).find("chunked") != std::string::npos) {
            chunked = true;
        }
        if (key == "content-length") {
            content_length = std::atoll(value.c_str());
        }
    }

    if (chunked) {
        std::string decoded;
        std::size_t position = 0;
        while (position < response.body.size()) {
            const std::size_t line_end = response.body.find("\r\n", position);
            if (line_end == std::string::npos) break;
            const std::size_t size =
                static_cast<std::size_t>(std::strtoul(response.body.c_str() + position, nullptr, 16));
            position = line_end + 2;
            if (size == 0) break;
            if (position + size > response.body.size()) {
                decoded.append(response.body, position, response.body.size() - position);
                break;
            }
            decoded.append(response.body, position, size);
            position += size + 2;  // skip the trailing CRLF
        }
        response.body = decoded;
    } else if (content_length >= 0 &&
               static_cast<std::size_t>(content_length) < response.body.size()) {
        // With `Connection: close` the server may pad; trust Content-Length.
        response.body.resize(static_cast<std::size_t>(content_length));
    }
    return response;
}

Response HttpClient::get(const std::string& path) const {
    return request("GET", path, "", "", "");
}

Response HttpClient::post_json(const std::string& path, const std::string& json) const {
    return request("POST", path, json, "application/json", "");
}

Response HttpClient::post_json_auth(const std::string& path, const std::string& json,
                                    const std::string& bearer_token) const {
    return request("POST", path, json, "application/json", bearer_token);
}

Response HttpClient::post_multipart(const std::string& path,
                                    const std::vector<MultipartField>& fields) const {
    // A fixed boundary is fine: the payload is a JPEG we produced ourselves, so
    // a collision is not a practical concern, and it keeps the client stateless.
    const std::string boundary = "----SIDBScannerBoundary7MA4YWxkTrZu0gW";
    std::string body;
    for (const MultipartField& field : fields) {
        body += "--" + boundary + "\r\n";
        if (field.is_file) {
            body += "Content-Disposition: form-data; name=\"" + field.name + "\"; filename=\"" +
                    field.filename + "\"\r\n";
            body += "Content-Type: " +
                    (field.content_type.empty() ? std::string("application/octet-stream")
                                                 : field.content_type) +
                    "\r\n\r\n";
            body += field.file_bytes;
            body += "\r\n";
        } else {
            body += "Content-Disposition: form-data; name=\"" + field.name + "\"\r\n\r\n";
            body += field.value;
            body += "\r\n";
        }
    }
    body += "--" + boundary + "--\r\n";
    return request("POST", path, body, "multipart/form-data; boundary=" + boundary, "");
}

bool HttpClient::is_reachable(const std::string& host, int port, int timeout_ms) {
    std::string error;
    Socket socket(connect_with_timeout(host, port, timeout_ms, error));
    if (socket.get() < 0) {
        SIDB_LOG(log::Level::Debug, kComponent, host + ":" + std::to_string(port) +
                                                " unreachable: " + error);
        return false;
    }
    return true;
}

}  // namespace sidb::http
