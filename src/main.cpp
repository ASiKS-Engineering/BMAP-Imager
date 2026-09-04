#define NOMINMAX
#define UNICODE
#define _UNICODE

#include <windows.h>
#include <winioctl.h>
#include <setupapi.h>
#include <wincrypt.h>

#include <algorithm>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <regex>
#include <sstream>
#include <string>
#include <vector>


struct Range {
    uint64_t first;
    uint64_t last;
    std::string checksum;
};

struct Bmap {
    uint64_t imageSize = 0;
    uint64_t blockSize = 0;
    uint64_t mappedBlocks = 0;
    std::vector<Range> ranges;
};


// -----------------------------------------------------------------------------
// Utility
// -----------------------------------------------------------------------------

static std::string lower(std::string s)
{
    std::transform(
        s.begin(),
        s.end(),
        s.begin(),
        [](unsigned char c) {
            return static_cast<char>(std::tolower(c));
        });

    return s;
}


static std::string errstr(DWORD error)
{
    LPWSTR message = nullptr;

    FormatMessageW(
        FORMAT_MESSAGE_ALLOCATE_BUFFER |
            FORMAT_MESSAGE_FROM_SYSTEM |
            FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr,
        error,
        0,
        reinterpret_cast<LPWSTR>(&message),
        0,
        nullptr);

    std::wstring wmessage =
        message ? message : L"unknown error";

    if (message) {
        LocalFree(message);
    }

    return std::string(
        wmessage.begin(),
        wmessage.end());
}


static bool parse_u64(
    const std::string& s,
    uint64_t& value)
{
    try {
        size_t pos = 0;

        value = std::stoull(
            s,
            &pos,
            10);

        return pos == s.size();
    }
    catch (...) {
        return false;
    }
}


// -----------------------------------------------------------------------------
// SHA-256
// -----------------------------------------------------------------------------

static std::string sha256_hex(
    const std::vector<uint8_t>& data)
{
    HCRYPTPROV provider = 0;
    HCRYPTHASH hash = 0;

    if (!CryptAcquireContextW(
            &provider,
            nullptr,
            nullptr,
            PROV_RSA_AES,
            CRYPT_VERIFYCONTEXT)) {
        return {};
    }

    if (!CryptCreateHash(
            provider,
            CALG_SHA_256,
            0,
            0,
            &hash)) {

        CryptReleaseContext(provider, 0);
        return {};
    }

    if (!CryptHashData(
            hash,
            data.data(),
            static_cast<DWORD>(data.size()),
            0)) {

        CryptDestroyHash(hash);
        CryptReleaseContext(provider, 0);
        return {};
    }

    BYTE digest[32];
    DWORD digestSize = sizeof(digest);

    bool ok = CryptGetHashParam(
        hash,
        HP_HASHVAL,
        digest,
        &digestSize,
        0);

    CryptDestroyHash(hash);
    CryptReleaseContext(provider, 0);

    if (!ok) {
        return {};
    }

    std::ostringstream out;

    out << std::hex
        << std::setfill('0');

    for (BYTE byte : digest) {
        out << std::setw(2)
            << static_cast<int>(byte);
    }

    return out.str();
}


static bool sha256_file_range(
    std::ifstream& file,
    uint64_t offset,
    uint64_t bytes,
    std::string& hex)
{
    HCRYPTPROV provider = 0;
    HCRYPTHASH hash = 0;

    if (!CryptAcquireContextW(
            &provider,
            nullptr,
            nullptr,
            PROV_RSA_AES,
            CRYPT_VERIFYCONTEXT)) {
        return false;
    }

    if (!CryptCreateHash(
            provider,
            CALG_SHA_256,
            0,
            0,
            &hash)) {

        CryptReleaseContext(provider, 0);
        return false;
    }

    constexpr size_t BUFFER_SIZE = 4 * 1024 * 1024;

    std::vector<char> buffer(BUFFER_SIZE);

    file.clear();

    file.seekg(
        static_cast<std::streamoff>(offset),
        std::ios::beg);

    if (!file) {
        CryptDestroyHash(hash);
        CryptReleaseContext(provider, 0);
        return false;
    }

    uint64_t remaining = bytes;

    while (remaining > 0) {
        size_t current =
            static_cast<size_t>(
                std::min<uint64_t>(
                    remaining,
                    BUFFER_SIZE));

        file.read(
            buffer.data(),
            static_cast<std::streamsize>(current));

        if (static_cast<size_t>(file.gcount()) != current) {
            CryptDestroyHash(hash);
            CryptReleaseContext(provider, 0);
            return false;
        }

        if (!CryptHashData(
                hash,
                reinterpret_cast<BYTE*>(buffer.data()),
                static_cast<DWORD>(current),
                0)) {

            CryptDestroyHash(hash);
            CryptReleaseContext(provider, 0);
            return false;
        }

        remaining -= current;
    }

    BYTE digest[32];
    DWORD digestSize = sizeof(digest);

    bool ok = CryptGetHashParam(
        hash,
        HP_HASHVAL,
        digest,
        &digestSize,
        0);

    CryptDestroyHash(hash);
    CryptReleaseContext(provider, 0);

    if (!ok) {
        return false;
    }

    std::ostringstream out;

    out << std::hex
        << std::setfill('0');

    for (BYTE byte : digest) {
        out << std::setw(2)
            << static_cast<int>(byte);
    }

    hex = out.str();

    return true;
}


// -----------------------------------------------------------------------------
// File / BMAP
// -----------------------------------------------------------------------------

static bool read_text(
    const std::wstring& path,
    std::string& text)
{
    std::ifstream file(
        std::filesystem::path(path),
        std::ios::binary);

    if (!file) {
        return false;
    }

    std::ostringstream output;
    output << file.rdbuf();

    text = output.str();

    return true;
}


static bool parse_bmap(
    const std::wstring& path,
    Bmap& bmap,
    std::string& why)
{
    std::string xml;

    if (!read_text(path, xml)) {
        why = "cannot open bmap";
        return false;
    }

    std::smatch match;

    const std::regex imageSizeRegex(
        R"(<ImageSize>\s*(\d+)\s*</ImageSize>)",
        std::regex::icase);

    const std::regex blockSizeRegex(
        R"(<BlockSize>\s*(\d+)\s*</BlockSize>)",
        std::regex::icase);

    const std::regex mappedBlocksRegex(
        R"(<MappedBlocksCount>\s*(\d+)\s*</MappedBlocksCount>)",
        std::regex::icase);

    if (!std::regex_search(
            xml,
            match,
            imageSizeRegex) ||
        !parse_u64(
            match[1].str(),
            bmap.imageSize)) {

        why = "missing/invalid ImageSize";
        return false;
    }

    if (!std::regex_search(
            xml,
            match,
            blockSizeRegex) ||
        !parse_u64(
            match[1].str(),
            bmap.blockSize) ||
        bmap.blockSize == 0) {

        why = "missing/invalid BlockSize";
        return false;
    }

    if (std::regex_search(
            xml,
            match,
            mappedBlocksRegex)) {

        parse_u64(
            match[1].str(),
            bmap.mappedBlocks);
    }

    const std::regex rangeRegex(
        R"(<Range\b[^>]*>\s*(\d+)\s*-\s*(\d+)\s*</Range>)",
        std::regex::icase);

    const std::regex checksumRegex(
        R"(chksum\s*=\s*["']([0-9a-fA-F]{64})["'])",
        std::regex::icase);

    auto iterator =
        std::sregex_iterator(
            xml.begin(),
            xml.end(),
            rangeRegex);

    const auto end =
        std::sregex_iterator();

    while (iterator != end) {
        const auto matchRange = *iterator;

        Range range{};

        if (!parse_u64(
                matchRange[1].str(),
                range.first) ||
            !parse_u64(
                matchRange[2].str(),
                range.last) ||
            range.first > range.last) {

            why = "invalid range";
            return false;
        }

        const size_t position =
            static_cast<size_t>(
                matchRange.position());

        const size_t tagStart =
            xml.rfind(
                "<Range",
                position);

        const size_t tagEnd =
            xml.find(
                '>',
                tagStart);

        if (tagStart != std::string::npos &&
            tagEnd != std::string::npos) {

            const std::string tagText =
                xml.substr(
                    tagStart,
                    tagEnd - tagStart + 1);

            std::smatch checksumMatch;

            if (std::regex_search(
                    tagText,
                    checksumMatch,
                    checksumRegex)) {

                range.checksum =
                    lower(
                        checksumMatch[1].str());
            }
        }

        bmap.ranges.push_back(range);

        ++iterator;
    }

    if (bmap.ranges.empty()) {
        why = "no Range entries";
        return false;
    }

    return true;
}


// -----------------------------------------------------------------------------
// Windows physical disk access
// -----------------------------------------------------------------------------

static HANDLE open_disk_query(int diskNumber)
{
    const std::wstring path =
        L"\\\\.\\PhysicalDrive" +
        std::to_wstring(diskNumber);

    return CreateFileW(
        path.c_str(),
        0,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        nullptr,
        OPEN_EXISTING,
        0,
        nullptr);
}


static HANDLE open_disk(
    int diskNumber,
    bool write)
{
    const std::wstring path =
        L"\\\\.\\PhysicalDrive" +
        std::to_wstring(diskNumber);

    DWORD access =
        write
            ? (GENERIC_READ | GENERIC_WRITE)
            : GENERIC_READ;

    return CreateFileW(
        path.c_str(),
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        nullptr,
        OPEN_EXISTING,
        FILE_FLAG_NO_BUFFERING |
            FILE_FLAG_WRITE_THROUGH,
        nullptr);
}


// -----------------------------------------------------------------------------
// Disk information
// -----------------------------------------------------------------------------

static bool get_disk_info(
    HANDLE handle,
    uint64_t& size,
    DWORD& sectorSize)
{
    size = 0;
    sectorSize = 512;

    //
    // Try IOCTL_DISK_GET_LENGTH_INFO first.
    //
    GET_LENGTH_INFORMATION lengthInfo{};
    DWORD returned = 0;

    if (DeviceIoControl(
            handle,
            IOCTL_DISK_GET_LENGTH_INFO,
            nullptr,
            0,
            &lengthInfo,
            sizeof(lengthInfo),
            &returned,
            nullptr)) {

        size =
            static_cast<uint64_t>(
                lengthInfo.Length.QuadPart);
    }

    //
    // Get sector size and, if necessary,
    // disk size from geometry.
    //
    DISK_GEOMETRY_EX geometry{};
    returned = 0;

    if (DeviceIoControl(
            handle,
            IOCTL_DISK_GET_DRIVE_GEOMETRY_EX,
            nullptr,
            0,
            &geometry,
            sizeof(geometry),
            &returned,
            nullptr)) {

        sectorSize =
            geometry.Geometry.BytesPerSector;

        if (size == 0) {
            size =
                static_cast<uint64_t>(
                    geometry.DiskSize.QuadPart);
        }

        return size != 0;
    }

    return size != 0;
}


// -----------------------------------------------------------------------------
// Raw I/O
// -----------------------------------------------------------------------------

static bool io_at(
    HANDLE handle,
    bool write,
    uint64_t offset,
    void* buffer,
    DWORD bytes)
{
    LARGE_INTEGER position;
    position.QuadPart =
        static_cast<LONGLONG>(offset);

    if (!SetFilePointerEx(
            handle,
            position,
            nullptr,
            FILE_BEGIN)) {

        return false;
    }

    DWORD transferred = 0;

    if (write) {
        return WriteFile(
            handle,
            buffer,
            bytes,
            &transferred,
            nullptr) &&
            transferred == bytes;
    }

    return ReadFile(
        handle,
        buffer,
        bytes,
        &transferred,
        nullptr) &&
        transferred == bytes;
}


// -----------------------------------------------------------------------------
// Range checksum
// -----------------------------------------------------------------------------

static bool range_checksum(
    std::ifstream& file,
    const Range& range,
    uint64_t blockSize,
    std::string& output)
{
    if (range.first >
        std::numeric_limits<uint64_t>::max() /
            blockSize) {

        return false;
    }

    const uint64_t offset =
        range.first * blockSize;

    const uint64_t blockCount =
        range.last - range.first + 1;

    if (blockCount >
        std::numeric_limits<uint64_t>::max() /
            blockSize) {

        return false;
    }

    const uint64_t bytes =
        blockCount * blockSize;

    return sha256_file_range(
        file,
        offset,
        bytes,
        output);
}


// -----------------------------------------------------------------------------
// Commands
// -----------------------------------------------------------------------------

static void usage()
{
    std::cout
        << "bmapflash.exe\n"
        << "\n"
        << "Usage:\n"
        << "  bmapflash list\n"
        << "  bmapflash info <image.img> <image.bmap>\n"
        << "  bmapflash flash <image.img> <image.bmap> PhysicalDriveN\n";
}


// -----------------------------------------------------------------------------
// Disk name parser
// -----------------------------------------------------------------------------

static bool parse_disk_name(
    const std::wstring& input,
    int& diskNumber)
{
    const std::wstring prefix =
        L"PhysicalDrive";

    if (input.rfind(prefix, 0) != 0) {
        return false;
    }

    try {
        size_t position = 0;

        diskNumber =
            std::stoi(
                input.substr(prefix.size()),
                &position);

        return
            position ==
                input.size() - prefix.size() &&
            diskNumber >= 0 &&
            diskNumber < 256;
    }
    catch (...) {
        return false;
    }
}


// -----------------------------------------------------------------------------
// LIST
// -----------------------------------------------------------------------------

static int cmd_list()
{
    std::wcout
        << L"Scanning PhysicalDrive0..31...\n";

    for (int number = 0; number < 32; ++number) {
        HANDLE handle =
            open_disk_query(number);

        if (handle == INVALID_HANDLE_VALUE) {
            const DWORD error =
                GetLastError();

            if (error != ERROR_FILE_NOT_FOUND &&
                error != ERROR_PATH_NOT_FOUND &&
                error != ERROR_INVALID_NAME) {

                std::wcout
                    << L"PhysicalDrive"
                    << number
                    << L": OPEN FAILED "
                    << error
                    << L" - "
                    << errstr(error).c_str()
                    << L"\n";
            }

            continue;
        }

        std::wcout
            << L"PhysicalDrive"
            << number
            << L": opened successfully\n";

        uint64_t size = 0;
        DWORD sectorSize = 512;

        if (get_disk_info(
                handle,
                size,
                sectorSize)) {

            std::wcout
                << L"  Size:   "
                << size
                << L" bytes\n"
                << L"  Sector: "
                << sectorSize
                << L" bytes\n";
        }
        else {
            const DWORD error =
                GetLastError();

            std::wcout
                << L"  QUERY FAILED "
                << error
                << L" - "
                << errstr(error).c_str()
                << L"\n";
        }

        CloseHandle(handle);
    }

    return 0;
}


// -----------------------------------------------------------------------------
// INFO
// -----------------------------------------------------------------------------

static int cmd_info(
    const std::wstring& imagePath,
    const std::wstring& bmapPath)
{
    Bmap bmap;
    std::string reason;

    if (!parse_bmap(
            bmapPath,
            bmap,
            reason)) {

        std::cerr
            << "BMAP error: "
            << reason
            << "\n";

        return 2;
    }

    std::ifstream image(
        std::filesystem::path(imagePath),
        std::ios::binary);

    if (!image) {
        std::cerr
            << "Cannot open image\n";

        return 2;
    }

    image.seekg(
        0,
        std::ios::end);

    uint64_t imageSize =
        static_cast<uint64_t>(
            image.tellg());

    uint64_t mappedBlocks = 0;

    for (const auto& range : bmap.ranges) {
        mappedBlocks +=
            range.last -
            range.first +
            1;
    }

    const uint64_t mappedBytes =
        mappedBlocks *
        bmap.blockSize;

    std::cout
        << "Image size: "
        << imageSize
        << " bytes\n"

        << "Block size: "
        << bmap.blockSize
        << " bytes\n"

        << "Ranges: "
        << bmap.ranges.size()
        << "\n"

        << "Mapped blocks: "
        << mappedBlocks
        << "\n"

        << "BMAP ImageSize: "
        << bmap.imageSize
        << "\n";

    if (imageSize != bmap.imageSize) {
        std::cerr
            << "ERROR: image size does not "
               "match BMAP.\n";

        return 2;
    }

    std::cout
        << "Mapped data: "
        << mappedBytes
        << " bytes ("
        << std::fixed
        << std::setprecision(2)
        << (100.0 *
            static_cast<double>(mappedBytes) /
            static_cast<double>(imageSize))
        << "%)\n";

    return 0;
}


// -----------------------------------------------------------------------------
// FLASH
// -----------------------------------------------------------------------------

static int cmd_flash(
    const std::wstring& imagePath,
    const std::wstring& bmapPath,
    const std::wstring& diskName)
{
    Bmap bmap;
    std::string reason;

    if (!parse_bmap(
            bmapPath,
            bmap,
            reason)) {

        std::cerr
            << "BMAP error: "
            << reason
            << "\n";

        return 2;
    }

    std::ifstream image(
        std::filesystem::path(imagePath),
        std::ios::binary);

    if (!image) {
        std::cerr
            << "Cannot open image\n";

        return 2;
    }

    image.seekg(
        0,
        std::ios::end);

    const uint64_t imageSize =
        static_cast<uint64_t>(
            image.tellg());

    if (imageSize != bmap.imageSize) {
        std::cerr
            << "ERROR: image size != "
               "BMAP ImageSize\n";

        return 2;
    }

    int diskNumber = 0;

    if (!parse_disk_name(
            diskName,
            diskNumber)) {

        std::cerr
            << "Target must look like "
               "PhysicalDrive5\n";

        return 2;
    }

    HANDLE disk =
        open_disk(
            diskNumber,
            true);

    if (disk == INVALID_HANDLE_VALUE) {
        const DWORD error =
            GetLastError();

        std::cerr
            << "Cannot open target: "
            << errstr(error)
            << "\n"
            << "Run as Administrator and "
               "ensure the disk is not in use.\n";

        return 3;
    }

    uint64_t diskSize = 0;
    DWORD sectorSize = 512;

    if (!get_disk_info(
            disk,
            diskSize,
            sectorSize)) {

        std::cerr
            << "Cannot query target size\n";

        CloseHandle(disk);
        return 3;
    }

    if (diskSize < imageSize) {
        std::cerr
            << "ERROR: target smaller "
               "than image\n";

        CloseHandle(disk);
        return 3;
    }

    if (bmap.blockSize % sectorSize != 0) {
        std::cerr
            << "ERROR: BMAP block size is "
               "not aligned to target sector size\n";

        CloseHandle(disk);
        return 3;
    }

    uint64_t totalBytes = 0;

    for (const auto& range : bmap.ranges) {
        totalBytes +=
            (range.last -
             range.first +
             1) *
            bmap.blockSize;
    }

    std::cout
        << "WARNING: this writes raw sectors to "
        << std::string(
               diskName.begin(),
               diskName.end())
        << ".\n"

        << "Existing data in mapped ranges "
           "will be destroyed.\n"

        << "Image: "
        << imageSize
        << " bytes\n"

        << "Target: "
        << diskSize
        << " bytes\n"

        << "Mapped: "
        << totalBytes
        << " bytes\n"

        << "Ranges: "
        << bmap.ranges.size()
        << "\n\n"

        << "Type YES to continue: ";

    std::string answer;

    std::getline(
        std::cin,
        answer);

    if (answer != "YES") {
        std::cout
            << "Aborted.\n";

        CloseHandle(disk);
        return 0;
    }

    constexpr DWORD CHUNK_SIZE =
        1024 * 1024;

    void* buffer =
        VirtualAlloc(
            nullptr,
            CHUNK_SIZE,
            MEM_COMMIT | MEM_RESERVE,
            PAGE_READWRITE);

    if (!buffer) {
        std::cerr
            << "VirtualAlloc failed\n";

        CloseHandle(disk);
        return 4;
    }

    uint64_t writtenBytes = 0;

    // -------------------------------------------------------------------------
    // Write mapped ranges
    // -------------------------------------------------------------------------

    for (size_t index = 0;
         index < bmap.ranges.size();
         ++index) {

        const auto& range =
            bmap.ranges[index];

        const uint64_t offset =
            range.first *
            bmap.blockSize;

        const uint64_t bytes =
            (range.last -
             range.first +
             1) *
            bmap.blockSize;

        // -------------------------------------------------------------
        // Verify image checksum before writing
        // -------------------------------------------------------------

        if (!range.checksum.empty()) {
            std::string calculated;

            if (!range_checksum(
                    image,
                    range,
                    bmap.blockSize,
                    calculated)) {

                std::cerr
                    << "Read/hash failed for "
                       "range "
                    << index
                    << "\n";

                VirtualFree(
                    buffer,
                    0,
                    MEM_RELEASE);

                CloseHandle(disk);
                return 5;
            }

            if (lower(calculated) !=
                range.checksum) {

                std::cerr
                    << "ERROR: image checksum "
                       "mismatch in range "
                    << index
                    << "\n";

                VirtualFree(
                    buffer,
                    0,
                    MEM_RELEASE);

                CloseHandle(disk);
                return 5;
            }
        }

        // -------------------------------------------------------------
        // Write range
        // -------------------------------------------------------------

        uint64_t remaining = bytes;
        uint64_t position = offset;

        while (remaining > 0) {
            const DWORD current =
                static_cast<DWORD>(
                    std::min<uint64_t>(
                        remaining,
                        CHUNK_SIZE));

            image.clear();

            image.seekg(
                static_cast<std::streamoff>(position),
                std::ios::beg);

            image.read(
                static_cast<char*>(buffer),
                static_cast<std::streamsize>(current));

            if (static_cast<DWORD>(
                    image.gcount()) != current) {

                std::cerr
                    << "\nImage read failed\n";

                VirtualFree(
                    buffer,
                    0,
                    MEM_RELEASE);

                CloseHandle(disk);
                return 5;
            }

            if (!io_at(
                    disk,
                    true,
                    position,
                    buffer,
                    current)) {

                const DWORD error =
                    GetLastError();

                std::cerr
                    << "\nDisk write failed "
                       "at "
                    << position
                    << ": "
                    << errstr(error)
                    << "\n";

                VirtualFree(
                    buffer,
                    0,
                    MEM_RELEASE);

                CloseHandle(disk);
                return 6;
            }

            position += current;
            remaining -= current;
            writtenBytes += current;

            const double percent =
                totalBytes == 0
                    ? 100.0
                    : 100.0 *
                          static_cast<double>(
                              writtenBytes) /
                          static_cast<double>(
                              totalBytes);

            std::cout
                << "\rWriting "
                << std::fixed
                << std::setprecision(1)
                << percent
                << "%"
                << std::flush;
        }

        // -------------------------------------------------------------
        // Verify target range
        // -------------------------------------------------------------

        HCRYPTPROV provider = 0;
        HCRYPTHASH hash = 0;

        if (!CryptAcquireContextW(
                &provider,
                nullptr,
                nullptr,
                PROV_RSA_AES,
                CRYPT_VERIFYCONTEXT) ||
            !CryptCreateHash(
                provider,
                CALG_SHA_256,
                0,
                0,
                &hash)) {

            std::cerr
                << "\nCrypto initialization failed\n";

            if (hash) {
                CryptDestroyHash(hash);
            }

            if (provider) {
                CryptReleaseContext(
                    provider,
                    0);
            }

            VirtualFree(
                buffer,
                0,
                MEM_RELEASE);

            CloseHandle(disk);
            return 7;
        }

        uint64_t verifyRemaining = bytes;
        uint64_t verifyPosition = offset;

        bool verifyOk = true;

        while (verifyRemaining > 0) {
            const DWORD current =
                static_cast<DWORD>(
                    std::min<uint64_t>(
                        verifyRemaining,
                        CHUNK_SIZE));

            if (!io_at(
                    disk,
                    false,
                    verifyPosition,
                    buffer,
                    current) ||
                !CryptHashData(
                    hash,
                    static_cast<BYTE*>(buffer),
                    current,
                    0)) {

                verifyOk = false;
                break;
            }

            verifyPosition += current;
            verifyRemaining -= current;
        }

        BYTE digest[32];
        DWORD digestSize = sizeof(digest);

        if (verifyOk) {
            verifyOk =
                !!CryptGetHashParam(
                    hash,
                    HP_HASHVAL,
                    digest,
                    &digestSize,
                    0);
        }

        std::ostringstream hashOutput;

        hashOutput
            << std::hex
            << std::setfill('0');

        if (verifyOk) {
            for (BYTE byte : digest) {
                hashOutput
                    << std::setw(2)
                    << static_cast<int>(byte);
            }
        }

        CryptDestroyHash(hash);
        CryptReleaseContext(
            provider,
            0);

        if (!verifyOk ||
            (!range.checksum.empty() &&
             lower(hashOutput.str()) !=
                 range.checksum)) {

            std::cerr
                << "\nVERIFY FAILED in range "
                << index
                << "\n";

            VirtualFree(
                buffer,
                0,
                MEM_RELEASE);

            CloseHandle(disk);
            return 8;
        }
    }

    // -------------------------------------------------------------------------
    // Flush
    // -------------------------------------------------------------------------

    if (!FlushFileBuffers(disk)) {
        const DWORD error =
            GetLastError();

        std::cerr
            << "\nWarning: FlushFileBuffers "
               "failed: "
            << errstr(error)
            << "\n";
    }

    std::cout
        << "\nFlash complete.\n"
        << "Verified "
        << writtenBytes
        << " mapped bytes.\n"
        << "Unmapped holes were not written.\n";

    VirtualFree(
        buffer,
        0,
        MEM_RELEASE);

    CloseHandle(disk);

    return 0;
}


// -----------------------------------------------------------------------------
// MAIN
// -----------------------------------------------------------------------------

int wmain(
    int argc,
    wchar_t** argv)
{
    if (argc < 2) {
        usage();
        return 1;
    }

    const std::wstring command =
        argv[1];

    if (command == L"list") {
        return cmd_list();
    }

    if (command == L"info" &&
        argc == 4) {

        return cmd_info(
            argv[2],
            argv[3]);
    }

    if (command == L"flash" &&
        argc == 5) {

        return cmd_flash(
            argv[2],
            argv[3],
            argv[4]);
    }

    usage();

    return 1;
}