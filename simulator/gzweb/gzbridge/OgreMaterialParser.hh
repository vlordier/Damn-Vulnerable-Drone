#ifndef GZBRIDGE_OGREMATERIALPARSER_HH_
#define GZBRIDGE_OGREMATERIALPARSER_HH_

#include <string>
#include <memory>

namespace gzweb
{
  class ConfigNode;
  class ConfigLoader;

  class OgreMaterialParser
  {
    public: OgreMaterialParser();

    public: virtual ~OgreMaterialParser();

    public: OgreMaterialParser(const OgreMaterialParser &) = delete;

    public: OgreMaterialParser &operator=(const OgreMaterialParser &) = delete;

    public: void Load(const std::string &_path);

    public: std::string GetMaterialAsJson() const;

    private: std::unique_ptr<ConfigLoader> configLoader;
  };

}

#endif
