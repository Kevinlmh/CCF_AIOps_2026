// Streaming CSV field-presence counter. Compile with: clang++ -O3 -std=c++17.
// Handles quoted commas, escaped quotes and quoted newlines. It never stores
// data rows, so the 96 GiB stage-1 source set can be audited with bounded RAM.

#include <cctype>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <unordered_map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

static bool complete_record(const std::string& record) {
    bool quoted = false;
    for (size_t i = 0; i < record.size(); ++i) {
        if (record[i] != '"') continue;
        if (quoted && i + 1 < record.size() && record[i + 1] == '"') {
            ++i;
        } else {
            quoted = !quoted;
        }
    }
    return !quoted;
}

static std::vector<std::string_view> fields(const std::string& record) {
    std::vector<std::string_view> result;
    bool quoted = false;
    size_t start = 0;
    for (size_t i = 0; i < record.size(); ++i) {
        if (record[i] == '"') {
            if (quoted && i + 1 < record.size() && record[i + 1] == '"') {
                ++i;
            } else {
                quoted = !quoted;
            }
        } else if (record[i] == ',' && !quoted) {
            result.emplace_back(record.data() + start, i - start);
            start = i + 1;
        }
    }
    result.emplace_back(record.data() + start, record.size() - start);
    for (auto& part : result) {
        while (!part.empty() && (std::isspace(static_cast<unsigned char>(part.front())) || part.front() == '"')) {
            part.remove_prefix(1);
        }
        while (!part.empty() && (std::isspace(static_cast<unsigned char>(part.back())) || part.back() == '"')) {
            part.remove_suffix(1);
        }
    }
    return result;
}

static bool missing(std::string_view value) {
    return value.empty() || value == "\\N" || value == "NULL" || value == "null" ||
           value == "None" || value == "none" || value == "NA" || value == "N/A" ||
           value == "nan" || value == "NaN";
}

int main(int argc, char** argv) {
    if (argc != 2 && argc != 4) {
        std::cerr << "usage: scan_csv_quality FILE.csv [--group FIELD]\n";
        return 2;
    }
    if (argc == 4 && std::string_view(argv[2]) != "--group") {
        std::cerr << "expected --group FIELD\n";
        return 2;
    }
    std::ifstream input(argv[1], std::ios::binary);
    if (!input) {
        std::cerr << "cannot open input\n";
        return 2;
    }
    std::vector<char> buffer(8 * 1024 * 1024);
    input.rdbuf()->pubsetbuf(buffer.data(), buffer.size());
    std::string record;
    if (!std::getline(input, record)) {
        std::cerr << "empty input\n";
        return 2;
    }
    auto header_views = fields(record);
    std::vector<std::string> header;
    for (auto value : header_views) header.emplace_back(value);
    if (!header.empty() && header[0].size() >= 3 &&
        static_cast<unsigned char>(header[0][0]) == 0xEF &&
        static_cast<unsigned char>(header[0][1]) == 0xBB &&
        static_cast<unsigned char>(header[0][2]) == 0xBF) {
        header[0].erase(0, 3);
    }
    std::vector<size_t> group_indexes;
    if (argc == 4) {
        std::string spec(argv[3]);
        size_t start = 0;
        do {
            const size_t stop = spec.find('+', start);
            const std::string field = spec.substr(start, stop - start);
            size_t found = header.size();
            for (size_t i = 0; i < header.size(); ++i) {
                if (header[i] == field) found = i;
            }
            if (found == header.size()) {
                std::cerr << "group field absent from header: " << field << "\n";
                return 2;
            }
            group_indexes.push_back(found);
            if (stop == std::string::npos) break;
            start = stop + 1;
        } while (true);
    }
    std::vector<uint64_t> absent(header.size(), 0);
    std::unordered_map<std::string, std::vector<uint64_t>> group_absent;
    uint64_t rows = 0, malformed = 0;
    while (std::getline(input, record)) {
        while (!complete_record(record)) {
            std::string continuation;
            if (!std::getline(input, continuation)) break;
            record += '\n';
            record += continuation;
        }
        auto values = fields(record);
        ++rows;
        if (values.size() != header.size()) {
            ++malformed;
            continue;
        }
        std::vector<uint64_t>* grouped = nullptr;
        if (!group_indexes.empty()) {
            std::string key;
            for (size_t i = 0; i < group_indexes.size(); ++i) {
                if (i) key += '|';
                key += values[group_indexes[i]];
            }
            auto& value = group_absent[key];
            if (value.empty()) value.resize(header.size() + 1, 0);
            ++value[0];
            grouped = &value;
        }
        for (size_t i = 0; i < values.size(); ++i) {
            const bool is_missing = missing(values[i]);
            absent[i] += is_missing;
            if (grouped) (*grouped)[i + 1] += is_missing;
        }
    }
    if (input.bad()) {
        std::cerr << "read error\n";
        return 2;
    }
    std::cout << "@\t" << rows << '\t' << malformed << '\n';
    for (size_t i = 0; i < header.size(); ++i) {
        std::cout << header[i] << '\t' << absent[i] << '\n';
    }
    for (const auto& item : group_absent) {
        std::cout << "#\t";
        for (char value : item.first) {
            if (value == '\n') std::cout << "\\n";
            else if (value == '\r') std::cout << "\\r";
            else if (value == '\t') std::cout << "\\t";
            else std::cout << value;
        }
        for (auto value : item.second) std::cout << '\t' << value;
        std::cout << '\n';
    }
}
